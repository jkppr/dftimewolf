# -*- coding: utf-8 -*-
"""This class maintains the internal dfTimewolf state.

Use it to track errors, abort on global failures, clean up after modules, etc.
"""

from concurrent.futures import ThreadPoolExecutor
import importlib
import logging
import threading
import traceback
from typing import Callable, Dict, List, Sequence, TYPE_CHECKING, Type, Any, TypeVar, cast # pylint: disable=line-too-long

from dftimewolf.config import Config
from dftimewolf.lib import errors, utils
from dftimewolf.lib.errors import DFTimewolfError
from dftimewolf.lib.modules import manager as modules_manager
from dftimewolf.lib.module import ThreadAwareModule

if TYPE_CHECKING:
  from dftimewolf.lib import module as dftw_module
  from dftimewolf.lib.containers import interface
T = TypeVar("T", bound="interface.AttributeContainer")  # pylint: disable=invalid-name,line-too-long

# TODO(tomchop): Consider changing this to `dftimewolf.state` if we ever need
# more granularity.
logger = logging.getLogger('dftimewolf')

NEW_ISSUE_URL = 'https://github.com/log2timeline/dftimewolf/issues/new'


class DFTimewolfState(object):
  """The main State class.

  Attributes:
    command_line_options (dict[str, str]): Command line options passed to
        dftimewolf.
    config (dftimewolf.config.Config): Class to be used throughout execution.
    errors (list[tuple[str, bool]]): errors generated by a module. These
        should be cleaned up after each module run using the CleanUp() method.
    global_errors (list[tuple[str, bool]]): the CleanUp() method moves non
        critical errors to this attribute for later reporting.
    input (list[str]): data that the current module will use as input.
    output (list[str]): data that the current module generates.
    recipe: (dict[str, str]): recipe declaring modules to load.
    store (dict[str, object]): arbitrary data for modules.
  """

  def __init__(self, config: Type[Config]) -> None:
    """Initializes a state."""
    super(DFTimewolfState, self).__init__()
    self.command_line_options = {}  # type: Dict[str, str]
    self._cache = {}  # type: Dict[str, str]
    self._module_pool = {}  # type: Dict[str, dftw_module.BaseModule]
    self._state_lock = threading.Lock()
    self._threading_event_per_module = {}  # type: Dict[str, threading.Event]
    self.config = config
    self.errors = []  # type: List[DFTimewolfError]
    self.global_errors = [] # type: List[DFTimewolfError]
    self.recipe = {} # type: Dict[str, Any]
    self.store = {}  # type: Dict[str, List[interface.AttributeContainer]]
    self.streaming_callbacks = {}  # type: Dict[Type[interface.AttributeContainer], List[Callable[[Any], Any]]]  # pylint: disable=line-too-long
    self._abort_execution = False

  def _InvokeModulesInThreads(self, callback: Callable[[Any], Any]) -> None:
    """Invokes the callback function on all the modules in separate threads.

    Args:
      callback (function): callback function to invoke on all the modules.
    """
    threads = []
    for module_definition in self.recipe['modules']:
      thread_args = (module_definition, )
      thread = threading.Thread(target=callback, args=thread_args)
      threads.append(thread)
      thread.start()

    for thread in threads:
      thread.join()

    self.CheckErrors(is_global=True)

  def ImportRecipeModules(self, module_locations: Dict[str, str]) -> None:
    """Dynamically loads the modules declared in a recipe.

    Args:
      module_location (dict[str, str]): A dfTimewolf module name - Python module
          mapping. e.g.:
            {'GRRArtifactCollector': 'dftimewolf.lib.collectors.grr_hosts'}

    Raises:
      errors.RecipeParseError: if a module requested in a recipe does not
          exist in the mapping.
    """
    for module in self.recipe['modules'] + self.recipe.get('preflights', []):
      name = module['name']
      if name not in module_locations:
        msg = (f'In {self.recipe["name"]}: module {name} cannot be found. '
               'It may not have been declared.')
        raise errors.RecipeParseError(msg)
      logger.debug('Loading module {0:s} from {1:s}'.format(
          name, module_locations[name]))

      location = module_locations[name]
      try:
        importlib.import_module(location)
      except ModuleNotFoundError as exception:
        msg = f'Cannot find Python module for {name} ({location}): {exception}'
        raise errors.RecipeParseError(msg)

  def LoadRecipe(self,
                 recipe: Dict[str, Any],
                 module_locations: Dict[str, str]) -> None:
    """Populates the internal module pool with modules declared in a recipe.

    Args:
      recipe (dict[str, Any]): recipe declaring modules to load.

    Raises:
      RecipeParseError: if a module in the recipe has not been declared.
    """
    self.recipe = recipe
    module_definitions = recipe.get('modules', [])
    preflight_definitions = recipe.get('preflights', [])
    self.ImportRecipeModules(module_locations)

    for module_definition in module_definitions + preflight_definitions:
      # Combine CLI args with args from the recipe description
      module_name = module_definition['name']
      module_class = modules_manager.ModulesManager.GetModuleByName(module_name)

      runtime_name = module_definition.get('runtime_name')
      if not runtime_name:
        runtime_name = module_name
      self._module_pool[runtime_name] = module_class(self, name=runtime_name)

  def FormatExecutionPlan(self) -> str:
    """Formats execution plan.

    Returns information about loaded modules and their corresponding arguments
    to stdout.

    Returns:
      str: String representation of loaded modules and their parameters.
    """
    plan = ""
    maxlen = 0

    modules = self.recipe.get('preflights', []) + self.recipe.get('modules', [])

    for module in modules:
      if not module['args']:
        continue
      spacing = len(max(module['args'].keys(), key=len))
      maxlen = maxlen if maxlen > spacing else spacing

    for module in modules:
      runtime_name = module.get('runtime_name')
      if runtime_name:
        plan += '{0:s} ({1:s}):\n'.format(runtime_name, module['name'])
      else:
        plan += '{0:s}:\n'.format(module['name'])

      new_args = utils.ImportArgsFromDict(
          module['args'], self.command_line_options, self.config)

      if not new_args:
        plan += '  *No params*\n'
      for key, value in new_args.items():
        plan += '  {0:s}{1:s}\n'.format(key.ljust(maxlen + 3), repr(value))

    return plan

  def LogExecutionPlan(self) -> None:
    """Logs the result of FormatExecutionPlan() using the base logger."""
    for line in self.FormatExecutionPlan().split('\n'):
      logger.debug(line)

  def AddToCache(self, name: str, value: Any) -> None:
    """Thread-safe method to add data to the state's cache.

    If the cached item is already in the cache it will be
    overwritten with the new value.

    Args:
      name (str): string with the name of the cache variable.
      value (object): the value that will be stored in the cache.
    """
    with self._state_lock:
      self._cache[name] = value

  def GetFromCache(self, name: str, default_value: Any=None) -> Any:
    """Thread-safe method to get data from the state's cache.

    Args:
      name (str): string with the name of the cache variable.
      default_value (object): the value that will be returned if
          the item does not exist in the cache. Optional argument
          and defaults to None.

    Returns:
      object: object from the cache that corresponds to the name, or
          the value of "default_value" if the cache does not contain
          the variable.
    """
    with self._state_lock:
      return self._cache.get(name, default_value)

  def StoreContainer(self, container: "interface.AttributeContainer") -> None:
    """Thread-safe method to store data in the state's store.

    Args:
      container (AttributeContainer): data to store.
    """
    with self._state_lock:
      self.store.setdefault(container.CONTAINER_TYPE, []).append(container)

  def GetContainers(self,
                    container_class: Type[T],
                    pop: bool=False) -> Sequence[T]:
    """Thread-safe method to retrieve data from the state's store.

    Args:
      container_class (type): AttributeContainer class used to filter data.
      pop (Optional[bool]): Whether to remove the containers from the state when
          they are retrieved.

    Returns:
      Collection[AttributeContainer]: attribute container objects provided in
          the store that correspond to the container type.
    """
    with self._state_lock:
      container_objects = cast(
          List[T], self.store.get(container_class.CONTAINER_TYPE, []))
      if pop:
        self.store[container_class.CONTAINER_TYPE] = []
      return tuple(container_objects)

  def _SetupModuleThread(self, module_definition: Dict[str, str]) -> None:
    """Calls the module's SetUp() function and sets a threading event for it.

    Callback for _InvokeModulesInThreads.

    Args:
      module_definition (dict[str, str]): recipe module definition.
    """
    module_name = module_definition['name']
    runtime_name = module_definition.get('runtime_name', module_name)
    logger.info('Setting up module: {0:s}'.format(runtime_name))
    new_args = utils.ImportArgsFromDict(
        module_definition['args'], self.command_line_options, self.config)
    module = self._module_pool[runtime_name]

    try:
      if isinstance(module, ThreadAwareModule):
        module.PreSetUp()
      module.SetUp(**new_args)
      if isinstance(module, ThreadAwareModule):
        module.PostSetUp()
    except errors.DFTimewolfError:
      msg = "A critical error occurred in module {0:s}, aborting execution."
      logger.critical(msg.format(module.name))
    except Exception as exception:  # pylint: disable=broad-except
      msg = 'An unknown error occurred in module {0:s}: {1!s}'.format(
          module.name, exception)
      logger.critical(msg)
      # We're catching any exception that is not a DFTimewolfError, so we want
      # to generate an error for further reporting.
      error = errors.DFTimewolfError(
          message=msg, name='state', stacktrace=traceback.format_exc(),
          critical=True, unexpected=True)
      self.AddError(error)

    self._threading_event_per_module[runtime_name] = threading.Event()
    self.CleanUp()

  def SetupModules(self) -> None:
    """Performs setup tasks for each module in the module pool.

    Threads declared modules' SetUp() functions. Takes CLI arguments into
    account when replacing recipe parameters for each module.
    """
    # Note that vars() copies the values of argparse.Namespace to a dict.
    self._InvokeModulesInThreads(self._SetupModuleThread)

  def _RunModuleThread(self, module_definition: Dict[str, str]) -> None:
    """Runs the module's Process() function.

    Callback for _InvokeModulesInThreads.

    Waits for any blockers to have finished before running Process(), then
    sets an Event flag declaring the module has completed.

    Args:
      module_definition (dict): module definition.
    """
    module_name = module_definition['name']
    runtime_name = module_definition.get('runtime_name', module_name)

    for dependency in module_definition['wants']:
      self._threading_event_per_module[dependency].wait()

    module = self._module_pool[runtime_name]

    # Abort processing if a module has had critical failures before.
    if self._abort_execution:
      logger.critical(
          'Aborting execution of {0:s} due to previous errors'.format(
              module.name))
      self._threading_event_per_module[runtime_name].set()
      self.CleanUp()
      return

    logger.info('Running module: {0:s}'.format(runtime_name))

    try:
      if isinstance(module, ThreadAwareModule):
        module.PreProcess()

        futures = []
        logger.info(
            'Running {0:d} threads, max {1:d} simultaneous for module {2:s}'\
            .format(
                len(self.GetContainers(module.GetThreadOnContainerType())),
                module.GetThreadPoolSize(),
                runtime_name))

        with ThreadPoolExecutor(max_workers=module.GetThreadPoolSize()) \
            as executor:
          for container in \
              self.GetContainers(module.GetThreadOnContainerType()):
            futures.append(
                executor.submit(module.Process, container))

        module.PostProcess()

        for fut in futures:
          if fut.exception():
            raise fut.exception() # type: ignore

        if not module.KeepThreadedContainersInState():
          self.GetContainers(module.GetThreadOnContainerType(), True)
      else:
        module.Process()
    except errors.DFTimewolfError:
      logger.critical(
          "Critical error in module {0:s}, aborting execution".format(
              module.name))
    except Exception as exception:  # pylint: disable=broad-except
      msg = 'An unknown error occurred in module {0:s}: {1!s}'.format(
          module.name, exception)
      logger.critical(msg)
      # We're catching any exception that is not a DFTimewolfError, so we want
      # to generate an error for further reporting.
      error = errors.DFTimewolfError(
          message=msg, name='state', stacktrace=traceback.format_exc(),
          critical=True, unexpected=True)
      self.AddError(error)

    logger.info('Module {0:s} finished execution'.format(runtime_name))
    self._threading_event_per_module[runtime_name].set()
    self.CleanUp()

  def RunPreflights(self) -> None:
    """Runs preflight modules."""
    for preflight_definition in self.recipe.get('preflights', []):
      preflight_name = preflight_definition['name']
      runtime_name = preflight_definition.get('runtime_name', preflight_name)

      args = preflight_definition.get('args', {})

      new_args = utils.ImportArgsFromDict(
          args, self.command_line_options, self.config)
      preflight = self._module_pool[runtime_name]
      try:
        preflight.SetUp(**new_args)
        preflight.Process()
      finally:
        self.CheckErrors(is_global=True)

  def CleanUpPreflights(self) -> None:
    """Executes any cleanup actions defined in preflight modules."""
    for preflight_definition in self.recipe.get('preflights', []):
      preflight_name = preflight_definition['name']
      runtime_name = preflight_definition.get('runtime_name', preflight_name)
      preflight = self._module_pool[runtime_name]
      try:
        preflight.CleanUp()
      finally:
        self.CheckErrors(is_global=True)

  def InstantiateModule(self, module_name: str) -> "dftw_module.BaseModule":
    """Instantiates an arbitrary dfTimewolf module.

    Args:
      module_name (str): The name of the module to instantiate.

    Returns:
      BaseModule: An instance of a dftimewolf Module, which is a subclass of
          BaseModule.
    """
    module_class: Type["dftw_module.BaseModule"]
    module_class = modules_manager.ModulesManager.GetModuleByName(module_name)
    return module_class(self)

  def RunModules(self) -> None:
    """Performs the actual processing for each module in the module pool."""
    self._InvokeModulesInThreads(self._RunModuleThread)

  def RegisterStreamingCallback(
      self,
      target: Callable[["interface.AttributeContainer"], Any],
      container_type: Type["interface.AttributeContainer"]) -> None:
    """Registers a callback for a type of container.

    The function to be registered should a single parameter of type
    interface.AttributeContainer.

    Args:
      target (function): function to be called.
      container_type (type[interface.AttributeContainer]): container type on
          which the callback will be called.
    """
    if container_type not in self.streaming_callbacks:
      self.streaming_callbacks[container_type] = []
    self.streaming_callbacks[container_type].append(target)

  def StreamContainer(self, container: "interface.AttributeContainer") -> None:
    """Streams a container to the callbacks that are registered to handle it.

    Args:
      container (interface.AttributeContainer): container instance that will be
          streamed to any registered callbacks.
    """
    for callback in self.streaming_callbacks.get(type(container), []):
      callback(container)

  def AddError(self, error: DFTimewolfError) -> None:
    """Adds an error to the state.

    Args:
      error (errors.DFTimewolfError): The dfTimewolf error to add.
    """
    if error.critical:
      self._abort_execution = True
    self.errors.append(error)

  def CleanUp(self) -> None:
    """Cleans up after running a module.

    The state's output becomes the input for the next stage. Any errors are
    moved to the global_errors attribute so that they can be reported at a
    later stage.
    """
    # Move any existing errors to global errors
    self.global_errors.extend(self.errors)
    self.errors = []

  def CheckErrors(self, is_global: bool=False) -> None:
    """Checks for errors and exits if any of them are critical.

    Args:
      is_global (Optional[bool]): True if the global_errors attribute should
          be checked. False if the error attribute should be checked.
    """
    error_objects = self.global_errors if is_global else self.errors
    critical_errors = False

    if error_objects:
      logger.error('dfTimewolf encountered one or more errors:')

    for index, error in enumerate(error_objects):
      logger.error('{0:d}: error from {1:s}: {2:s}'.format(
          index+1, error.name, error.message))
      if error.stacktrace:
        for line in error.stacktrace.split('\n'):
          logger.error(line)
      if error.critical:
        critical_errors = True

    if any(error.unexpected for error in error_objects):
      logger.critical('One or more unexpected errors occurred.')
      logger.critical(
          'Please consider opening an issue: {0:s}'.format(NEW_ISSUE_URL))

    if critical_errors:
      raise errors.CriticalError('Critical error found. Aborting.')

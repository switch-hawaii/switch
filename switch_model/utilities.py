# Copyright (c) 2015-2020 The Switch Authors. All rights reserved.
# Licensed under the Apache License, Version 2.0, which is in the LICENSE file.

"""
Utility functions for Switch.
"""
from __future__ import print_function, division

import argparse
import datetime
import importlib
import os
import re
import sys
import logging
import time
import types
import textwrap

from pyomo.environ import *
import pyomo.opt

try:
    # sentinel for sets with no dimension specified in Pyomo 5.7+
    from pyomo.core.base.set import UnknownSetDimen
except ImportError:
    UnknownSetDimen = object()  # shouldn't ever match

# Define string_types (same as six.string_types). This is useful for
# distinguishing between strings and other iterables.
try:
    # Python 2
    string_types = (basestring,)
except NameError:
    # Python 3
    string_types = (str,)

def define_AbstractModel(*module_list, **kwargs):
    # stub to provide old functionality as we move to a simpler calling convention
    args = kwargs.get("args", sys.argv[1:])
    return create_model(module_list, args)

class SwitchAbstractModel(AbstractModel):
    """
    Subclass of standard Pyomo ConcreteModel with methods to implement Switch-
    specific behavior (initializing via modules, etc.).
    """
    def __init__(self, module_list=None, args=sys.argv[1:], logger=None):
        """
        Construct a customized Pyomo AbstractModel using the Switch modules or
        packages in the given list.

        This is implemented as calling the following functions for each module
        that has them defined:

        define_dynamic_lists(model): Add lists to the model that other modules
        can register with. Used for power balance equations, cost components of
        the objective function, etc.

        define_components(model): Add components to the model object
        (parameters, sets, decisions variables, expressions, and/or
        constraints). Also register with relevant dynamic_lists.

        define_dynamic_components(model): Add dynamic components to the model
        that depend on the contents of dyanmics lists. Power balance constraints
        and the objective function are defined in this manner.

        See financials and balancing.load_zones for examples of dynamic
        definitions.

        All modules can request access to command line parameters and set their
        default values for those options. If this codebase is being used more
        like a library than a stand-alone executable, this behavior can cause
        problems. For example, running this model with PySP's runph tool will
        cause errors where a runph argument such as --instance-directory is
        unknown to the switch modules, so parse_args() generates an error. This
        behavior can be avoided calling this function with an empty list for
        args: create_model(module_list, args=[])

        """
        # do standard Pyomo initialization
        AbstractModel.__init__(self)

        # Load modules
        if module_list is None:
            import switch_model.solve
            module_list = switch_model.solve.get_module_list(args)
        self.module_list = module_list
        for m in module_list:
            importlib.import_module(m)

        # Each model usually has its own logger, passed in by
        # switch_model.solve, because users may specify different logging
        # settings for each model. If not provided, we attach a default logger,
        # since all modules assume there's one in place.
        # (This is used to maintain consistent logging level throughout the
        # model life cycle. In the future it could be used to log different
        # models to different files. Currently that is handled by redirecting
        # all output to the right file via a wrapper around any code that may
        # produce output, to ensure we catch print() calls (deprecated) and
        # messages from Pyomo and its solver.
        if logger is None:
            logger = logging.getLogger("Switch Default Logger")
        self.logger = logger

        # Define and parse model configuration options
        argparser = _ArgumentParser(allow_abbrev=False)
        for module in self.get_modules():
            if hasattr(module, 'define_arguments'):
                module.define_arguments(argparser)
        self.options = argparser.parse_args(args)

        # Apply verbose flag to support code that still uses it (newer code should
        # use model.logger.isEnabledFor(logging.LEVEL)
        self.options.verbose = self.logger.isEnabledFor(logging.INFO)

        # Define model components
        for module in self.get_modules():
            if hasattr(module, 'define_dynamic_lists'):
                module.define_dynamic_lists(self)
        for module in self.get_modules():
            if hasattr(module, 'define_components'):
                module.define_components(self)
        for module in self.get_modules():
            if hasattr(module, 'define_dynamic_components'):
                module.define_dynamic_components(self)


    def get_modules(self):
        """ Return a list of loaded module objects for this model. """
        for m in self.module_list:
            yield sys.modules[m]


    def min_data_check(self, *mandatory_components):
        """
        This function checks that an instance of Pyomo abstract model has
        mandatory components defined. If a user attempts to create an instance
        without defining all of the necessary data, this will produce fatal
        errors with clear messages stating specifically what components have
        missing data.

        Without this check, Switch gives fatal errors if the users forgets to
        specify data for a component that doesn't have a default value, but the
        error message is obscure and references the first code that tries to
        reference the component with missing data.

        BuildCheck's message lists the name of the check that failed, but
        doesn't provide mechanisms for printing a specific error message. Just
        printing to screen is easy to miss, so we raise a ValueError with a
        clear and specific message.
        """
        try:
            self.__num_min_data_checks += 1
        except AttributeError:
            self.__num_min_data_checks = 0 # initialize
        new_data_check_name = "min_data_check_" + str(self.__num_min_data_checks)
        setattr(
            self,
            new_data_check_name,
            BuildCheck(
                rule=lambda m: check_mandatory_components(m, *mandatory_components)
            )
        )


    def load_inputs(self, inputs_dir=None, attach_data_portal=True):
        """
        Load input data using the appropriate modules and return a model
        instance. This is implemented by calling the load_inputs() function of
        each module, if the module has that function.
        """
        if inputs_dir is None:
            inputs_dir = getattr(self.options, "inputs_dir", "inputs")

        # Load data; add a fancier load function to the data portal
        timer = StepTimer()
        data = DataPortal(model=self)
        data.load_aug = types.MethodType(load_aug, data)
        for module in self.get_modules():
            if hasattr(module, 'load_inputs'):
                module.load_inputs(self, data, inputs_dir)
        if self.options.verbose:
            print("Data read in {:.2f} s.\n".format(timer.step_time()))

        if self.logger.isEnabledFor(logging.DEBUG):
            instance = self.create_instance(data, report_timing=True)
        # elif self.logger.isEnabledFor(logging.INFO):
        #     with TimingLineCounter(self):
        #         instance = self.create_instance(data, report_timing=True)
        else:
            instance = self.create_instance(data, report_timing=False)

        if attach_data_portal:
            instance.DataPortal = data

        if self.options.verbose:
            print("Instance created from data in {:.2f} s.\n".format(timer.step_time()))

        return instance


    def create_instance(*args, **kwargs):
        """
        Use standard Pyomo create_instance method, then convert to
        SwitchConcreteModel

        Pyomo deepcopies the AbstractModel during create_instance and then
        reassigns its __class__ as ConcreteModel before returning it (with a
        note that "It is absolutely crazy that this is allowed in Python"). This
        doesn't give a natural way to use our subclass of ConcreteModel (with
        pre_solve and post_solve methods). So we just use the same trick again
        and reassign as SwitchConcreteModel
        """
        instance = AbstractModel.create_instance(*args, **kwargs)
        instance.__class__ = SwitchConcreteModel
        return instance


class SwitchConcreteModel(ConcreteModel):
    """
    Subclass of standard Pyomo ConcreteModel with methods to implement Switch-
    specific behavior (pre_solve, post_solve, has_discrete_variables).
    """

    get_modules = SwitchAbstractModel.get_modules

    def has_discrete_variables(model):
        all_elements = lambda v: v.itervalues() if v.is_indexed() else [v]
        return any(
            v.is_binary() or v.is_integer()
            for variable in model.component_objects(Var, active=True)
            for v in all_elements(variable)
        )


    def pre_solve(self, outputs_dir=None):
        """
        Call pre-solve function (if present) in all modules used to compose this
        model. This method can be used to adjust the instance after it is
        created and before it is solved.
        """
        for module in self.get_modules():
            if hasattr(module, 'pre_solve'):
                module.pre_solve(self)


    def post_solve(self, outputs_dir=None):
        """
        Call post-solve function (if present) in all modules used to compose
        this model. This method can be used to report or save results from the
        solved model.
        """
        if outputs_dir is None:
            outputs_dir = getattr(self.options, "outputs_dir", "outputs")
        if not os.path.exists(outputs_dir):
            os.makedirs(outputs_dir)

        for module in self.get_modules():
            if hasattr(module, 'post_solve'):
                module.post_solve(self, outputs_dir)


def create_model(*args, **kwargs):
    """ Stub function to implement old functionality, now achieved via subclass.
    """
    return SwitchAbstractModel(*args, **kwargs)


def make_iterable(item):
    """Return an iterable for the one or more items passed."""
    if isinstance(item, string_types):
        i = iter([item])
    else:
        try:
            # check if it's iterable
            i = iter(item)
        except TypeError:
            i = iter([item])
    return i

class StepTimer(object):
    """
    Keep track of elapsed time for steps of a process.
    Use timer = StepTimer() to create a timer, then retrieve elapsed time and/or
    reset the timer at each step by calling timer.step_time()
    """
    def __init__(self):
        self.start_time = time.time()
    def step_time(self):
        """
        Reset timer to current time and return time elapsed since last step.
        """
        last_start = self.start_time
        self.start_time = now = time.time()
        return now - last_start



def save_inputs_as_dat(model, instance, save_path="inputs/complete_inputs.dat",
    exclude=[], sorted_output=False):
    """
    Save input data to a .dat file for use with PySP or other command line
    tools that have not been fully integrated with DataPortal.
    SYNOPSIS:
        save_inputs_as_dat(model, instance, save_path)
    """
    # helper function to convert values to strings,
    # putting quotes around values that start as strings
    quote_str = lambda v: '"{}"'.format(v) if isinstance(v, string_types) else '{}'.format(str(v))
    # helper function to create delimited lists from single items or iterables of any data type
    from switch_model.reporting import make_iterable
    join_space = lambda items: ' '.join(map(str, make_iterable(items)))  # space-separated list
    join_comma = lambda items: ','.join(map(str, make_iterable(items)))  # comma-separated list

    with open(save_path, "w") as f:
        for component_name in instance.DataPortal.data():
            if component_name in exclude:
                continue    # don't write data for components in exclude list
                            # (they're in scenario-specific files)
            component = getattr(model, component_name)
            comp_class = type(component).__name__
            component_data = instance.DataPortal.data(name=component_name)
            if comp_class == 'SimpleSet' or comp_class == 'OrderedSimpleSet':
                f.write(
                    "set {} := {};\n"
                    .format(component_name, join_space(component_data))
                )
            elif comp_class == 'IndexedParam':
                if component_data:  # omit components for which no data were provided
                    f.write("param {} := \n".format(component_name))
                    for key, value in (
                        sorted(iteritems(component_data))
                        if sorted_output
                        else iteritems(component_data)
                    ):
                        f.write(" {} {}\n".format(join_space(key), quote_str(value)))
                    f.write(";\n")
            elif comp_class == 'SimpleParam':
                f.write("param {} := {};\n".format(component_name, component_data))
            elif comp_class == 'IndexedSet':
                for key, vals in iteritems(component_data):
                    f.write(
                        "set {}[{}] := {};\n"
                        .format(component_name, join_comma(key), join_space(vals))
                    )
            else:
                raise ValueError(
                    "Error! Component type {} not recognized for model element '{}'.".
                    format(comp_class, component_name))


def unwrap(message):
    return (
        textwrap.dedent(message)
        .replace(' \n', ' ')
        .replace('\n', ' ')
        .strip()
    )


def check_mandatory_components(model, *mandatory_components):
    """
    Checks whether mandatory elements of a Pyomo model are populated,
    and returns a clear error message if they don't exist.

    Typically, this method is not used directly. Instead, the
    min_data_check() method will set up a BuildCheck that uses this
    function.

    If an argument is a set, it must have non-zero length.

    If an argument is an indexed parameter, it must have a value for
    every index in the indexed set. Do not use this for indexed params
    that have default values. If the set indexing a param is not
    mandatory and is empty, then the indexed parameter may be empty as
    well.

    If an argument is a simple parameter, it must have a value.

    This does not work with indexed sets.
    """

    for component_name in mandatory_components:
        obj = getattr(model, component_name)
        o_class = type(obj).__name__
        if o_class == 'SimpleSet' or o_class == 'OrderedSimpleSet':
            if len(obj) == 0:
                raise ValueError(
                    "No data is defined for the mandatory set '{}'.".
                    format(component_name))
        elif o_class == 'IndexedParam':
            if len(obj) != len(obj.index_set()):
                missing_index_elements = [
                    k for k in obj.index_set() if k not in obj
                ]
                raise ValueError(
                    "Values are not provided for every element of the "
                    "mandatory parameter '{}'. "
                    "Missing data for {} values, including: {}"
                    .format(
                        component_name, len(missing_index_elements),
                        missing_index_elements[:10]
                    )
                )
        elif o_class == 'IndexedSet':
            if len(obj) != len(obj.index_set()):
                raise ValueError(
                    ("Sets are not defined for every index of " +
                     "the mandatory indexed set '{}'").format(component_name))
        elif o_class == 'SimpleParam':
            if obj.value is None:
                raise ValueError(
                    "Value not provided for mandatory parameter '{}'".
                    format(component_name))
        else:
            raise ValueError(
                "Error! Object type {} not recognized for model element '{}'.".
                format(o_class, component_name))
    return True


class InputError(Exception):
    """Exception raised for errors in the input.

    Attributes:
        expression -- input expression in which the error occurred
        message -- explanation of the error
    """

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)

def apply_input_aliases(switch_data, path):
    """
    Translate filenames based on --input-alias[es] arguments.

    Filename substitutions are specified like
    --input-aliases ev_share.csv=ev_share.ev_flat.csv rps.csv=rps.2030.csv

    Filename 'none' will be converted to an empty string and usually be ignored.

    This enables use of alternative files to study sensitivities without
    creating complete input directories for each permutation.
    """
    try:
        file_aliases = switch_data.file_aliases
    except AttributeError:
        file_aliases = switch_data.file_aliases = {
            standard: alternative
            for standard, alternative in (
                pair.split('=') for pair in switch_data._model.options.input_aliases
            )
        }

    root, filename = os.path.split(path)
    if filename in file_aliases:
        old_path = path
        if file_aliases[filename].lower() == 'none':
            path = ''
        else:
            # Note: We could use os.path.normpath() to clean up paths like
            # 'inputs/../inputs_alt', but leaving them as-is may make it more
            # clear that an alias is in use if errors crop up later.
            path = os.path.join(root, file_aliases[filename])
            if not os.path.isfile(path):
                # catch alias naming errors (should always point to a real file)
                raise ValueError(
                    'Alias "{}" specified for file "{}" does not exist. '
                    'Specify {}=none if you want to supply no data.'
                    .format(path, old_path, filename)
                )
        switch_data._model.logger.info('Applying alias {}={}'.format(old_path, path))

    return path

def load_aug(switch_data, optional=False, optional_params=[], **kwargs):
    """
    This is a wrapper for the DataPortal object that accepts additional
    keywords to allow optional files, optional columns, and auto-select
    columns based on parameter names. The name is an abbreviation of
    load_augmented.

    * optional: Indicates the input file is entirely optional. If absent, the
    sets and/or parameters will either be blank or set to their default values
    as defined in the model.
    * optional_params: Indicates specific parameter columns are optional, and
    will be skipped during loading if they are not present in the input file.
    All params in optional files are added to this list automatically.
    optional_params are ignored for `.dat` files (rarely used).

    To do:
    * Come up with a better name for this function.
    * Streamline the API so each param is specified exactly once, either in
    param or optional_param, and use the same style for each (component
    objects rather than component names). Alternatively, have an option to
    auto-detect whether a param is optional based on whether it has a default
    value specified.
    * Replace this function with more auto-detection. Allow user to specify
    filename when defining parameters and sets. Also allow user to specify the
    name(s) of the column(s) in each set. Then use those automatically to pull
    data from the right file (and to write correct index column names in the
    generic output files). This will simplify code and ease comprehension
    (user can see immediately where the data come from for each component).
    This can also support auto-documenting of parameters and input files.
    * Maybe each input file should have the same name as the matching index set?
      generation_projects_info -> generation_projects
      gen_build_predetermined -> predetermined_gen_bld_yrs
      gen_build_costs -> gen_bld_yrs
    """

    # TODO:
    # Allow user to specify filename when defining parameters and sets.
    # Also allow user to specify the name(s) of the column(s) in each set.
    # Then use those automatically to pull data from the right file (and to
    # write correct index column names in the generic output files).
    # This will simplify code and ease comprehension (user can see
    # immediately where the data come from for each component). This can
    # also support auto-documenting of parameters and input files.

    # convert filename if needed
    kwargs['filename'] = apply_input_aliases(switch_data, kwargs['filename'])
    # store filename in local variable for easier access
    path = kwargs['filename']

    # catch obsolete auto_select argument (not used in 2.0.6 and later)
    for a in ['auto_select', 'autoselect']:
        if a in kwargs:
            del kwargs[a]
            # TODO: receive a reference to the model and use the logger for this
            print(
                "WARNING: obsolete argument {} ignored while reading {}. "
                "Please remove this from your code. Columns are always "
                "auto-selected now unless a 'select' argument is passed."
                .format(a, path)
            )

    # Skip if an optional file is unavailable
    if optional and not os.path.isfile(path):
        return

    # If this is a .dat file, then skip the rest of this fancy business; we'll
    # only check if the file is missing and optional for .csv files.
    filename, extension = os.path.splitext(path)
    if extension == '.dat':
        switch_data.load(**kwargs)
        return

    # copy optional_params to avoid side-effects when the list is altered below
    optional_params=list(optional_params)
    # Parse header and first row
    with open(path) as infile:
        headers_line = infile.readline()
        second_line = infile.readline()
    file_is_empty = (headers_line == '')
    file_has_no_data_rows = (second_line == '')
    suffix = path.split('.')[-1]
    if suffix in {'tab', 'tsv'}:
        separator = '\t'
    elif suffix == 'csv':
        separator = ','
    else:
        raise InputError('Unrecognized file type for input file {}'.format(path))
    # TODO: parse this more formally, e.g. using csv module
    headers = headers_line.strip().split(separator)
    # Skip if the file is empty.
    if optional and file_is_empty:
        return
    # Try to get a list of parameters. If param was given as a
    # singleton or a tuple, make it into a list that can be edited.
    params = []
    if 'param' in kwargs:
        # Tuple -> list
        if isinstance(kwargs['param'], tuple):
            kwargs['param'] = list(kwargs['param'])
        # Singleton -> list
        elif not isinstance(kwargs['param'], list):
            kwargs['param'] = [kwargs['param']]
        params = kwargs['param']
    # optional_params may include Param objects instead of names. In
    # those cases, convert objects to names.
    for (i, p) in enumerate(optional_params):
        if not isinstance(p, string_types):
            optional_params[i] = p.name
    # Expand the list of optional parameters to include any parameter that has
    # default() defined or that comes from an optional table. We also allow an
    # explicit list of optional parameters to support parameters like
    # gen_unit_size, which doesn't have a default value because it is undefined
    # for generators for which it does not apply.
    for p in params:
        if (optional or p.default() is not None) and p.name not in optional_params:
            optional_params.append(p.name)
    # How many index columns do we expect?
    # Grab the dimensionality of the index param if it was provided.
    if 'index' in kwargs:
        num_indexes = kwargs['index'].dimen
    # Next try the first parameter's index.
    elif len(params) > 0:
        try:
            num_indexes = params[0].index_set().dimen
        except (ValueError, AttributeError):
            num_indexes = 0
    # Default to 0 if both methods failed.
    else:
        num_indexes = 0

    if num_indexes is UnknownSetDimen:
        # Pyomo 5.7 and later use a sentinel and don't set the dimension
        # until later (construction time?) if no dimension is specified,
        # so we have to apply the default here
        num_indexes = 1

    # Make a select list if requested. Assume the left-most columns are
    # indexes and that other columns are named after their parameters.
    # Maybe this could be extended to use a standard prefix for each data file?
    # e.g., things related to regional fuel market supply tiers (indexed by RFM_SUPPLY_TIER)
    # could all get the prefix "rfm_supply_tier_". Then they could get shorter names
    # within the file (e.g., "cost" and "limit"). We could also require the data file
    # to be called "rfm_supply_tier.csv" for greater consistency/predictability.
    if 'select' not in kwargs:
        kwargs['select'] = headers[0:num_indexes] + [p.name for p in params]
    # Check to see if expected column names are in the file. If a column
    # name is missing and its parameter is optional, then drop it from
    # the select & param lists.
    if isinstance(kwargs['select'], tuple):
        kwargs['select'] = list(kwargs['select'])
    del_items = []
    for (i, col) in enumerate(kwargs['select']):
        p_i = i - num_indexes
        if col not in headers:
            if(len(params) > p_i >= 0 and
               params[p_i].name in optional_params):
                del_items.append((i, p_i))
            else:
                raise InputError(
                    'Column {} not found in file {}.'
                    .format(col, path))
    # When deleting entries from select & param lists, go from last
    # to first so that the indexes won't get messed up as we go.
    del_items.sort(reverse=True)
    for (i, p_i) in del_items:
        del kwargs['select'][i]
        del kwargs['param'][p_i]

    if optional and file_has_no_data_rows:
        # Skip the file.  Note that we are only doing this after having
        # validated the file's column headings.
        return
    # All done with cleaning optional bits. Pass the updated arguments
    # into the DataPortal.load() function.
    switch_data.load(**kwargs)


# Define an argument parser that accepts the allow_abbrev flag to
# prevent partial matches, even on versions of Python before 3.5.
# See https://bugs.python.org/issue14910
# This is needed because the parser may sometimes be called with only a subset
# of the eventual argument list (e.g., to parse module-related arguments before
# loading the modules and adding their arguments to the list), and without this
# flag, the parser could match arguments that are meant to be used later
# (It's not likely, but for example if the user specifies a flag "--exclude",
# which will be consumed by one of their modules, the default parser would
# match that to "--exclude-modules" during the early, partial parse.)
if sys.version_info >= (3, 5):
    _ArgumentParserAllowAbbrev = argparse.ArgumentParser
else:
    # patch ArgumentParser to accept the allow_abbrev flag
    # (works on Python 2.7 and maybe others)
    class _ArgumentParserAllowAbbrev(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs):
            if not kwargs.get("allow_abbrev", True):
                if hasattr(self, "_get_option_tuples"):
                    # force self._get_option_tuples to return an empty list (of partial matches)
                    # see https://bugs.python.org/issue14910#msg204678
                    def new_get_option_tuples(self, option_string):
                        return []
                    self._get_option_tuples = types.MethodType(new_get_option_tuples, self)
                else:
                    raise RuntimeError(
                        "Incompatible argparse module detected. This software requires "
                        "Python 3.5 or later, or an earlier version of argparse that defines "
                        "ArgumentParser._get_option_tuples()"
                    )
            # consume the allow_abbrev argument if present
            kwargs.pop("allow_abbrev", None)
            return argparse.ArgumentParser.__init__(self, *args, **kwargs)

class ExtendAction(argparse.Action):
    """Create or extend list with the provided items"""
    # from https://stackoverflow.com/a/41153081/3830997
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest) or []
        items.extend(values)
        setattr(namespace, self.dest, items)

class IncludeAction(argparse.Action):
    """Flag the specified items for inclusion in the model"""
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest) or []
        items.append(('include', values))
        setattr(namespace, self.dest, items)
class ExcludeAction(argparse.Action):
    """Flag the specified items for exclusion from the model"""
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest) or []
        items.append(('exclude', values))
        setattr(namespace, self.dest, items)

# Test whether we need to issue warnings about the Python parsing bug.
# (applies to at least Python 2.7.11 and 3.6.2)
# This bug messes up solve-scenarios if the user specifies
# --scenario x --solver-options-string="a=b c=d"
test_parser = argparse.ArgumentParser()
test_parser.add_argument('--arg1', nargs='+', default=[])
bad_equal_parser = (
    len(test_parser.parse_known_args(['--arg1', 'a', '--arg2=a=1 b=2'])[1])
    == 0
)

# TODO: merge the _ArgumentParserAllowAbbrev code into this class
class _ArgumentParser(_ArgumentParserAllowAbbrev):
    """
    Custom version of ArgumentParser:
    - warns about a bug in standard Python ArgumentParser for --arg="some words"
    - allows use of 'extend', 'include' and 'exclude' actions to accumulate lists
      with multiple calls
    """
    def __init__(self, *args, **kwargs):
        super(_ArgumentParser, self).__init__(*args, **kwargs)
        self.register('action', 'extend', ExtendAction)
        self.register('action', 'include', IncludeAction)
        self.register('action', 'exclude', ExcludeAction)

    def parse_known_args(self, args=None, namespace=None):
        # parse_known_args parses arguments like --list-arg a b --other-arg="something with space"
        # as list_arg=['a', 'b', '--other-arg="something with space"'].
        # See https://bugs.python.org/issue34390.
        # We issue a warning to avoid this.
        if bad_equal_parser and args is not None:
            for a in args:
                if a.startswith('--') and '=' in a:
                    print(
                        "Warning: argument '{}' may be parsed incorrectly. It is "
                        "safer to use ' ' instead of '=' as a separator."
                        .format(a)
                    )
                    time.sleep(2)  # give users a chance to see it
        return super(_ArgumentParser, self).parse_known_args(args, namespace)


def approx_equal(a, b, tolerance=0.01):
    return abs(a-b) <= (abs(a) + abs(b)) / 2.0 * tolerance


def default_solver():
    return pyomo.opt.SolverFactory('glpk')


def warn(message):
    """
    Send warning message to sys.stderr.
    Unlike warnings.warn, this does not add the current line of code to the message.
    TODO: replace all calls to this with model.logger.warn()
    """
    sys.stderr.write("WARNING: " + message + '\n')


class TeeStream(object):
    """
    Virtual stream that writes output to both stream1 and stream2. Attributes
    of stream1 will be reported to callers if needed. For example, specifying
    `sys.stdout=TeeStream(sys.stdout, log_file_handle)` will copy
    output destined for sys.stdout to log_file_handle as well.
    """
    def __init__(self, stream1, stream2):
        self.stream1 = stream1
        self.stream2 = stream2
    def __getattr__(self, *args, **kwargs):
        """
        Provide stream1 attributes when attributes are requested for this class.
        This supports code that assumes sys.stdout is an object with its own
        methods, etc.
        """
        return getattr(self.stream1, *args, **kwargs)
    def write(self, text):
        for f in [self.stream1, self.stream2]:
            if f.isatty() or '\b' not in text:
                # normal processing
                f.write(text)
            else:
                for c in text:
                    if c == '\b':
                        # move one character before current position, to
                        # overwrite current character like a terminal
                        # (Python can't do f.seek(-1, 1) for some reason.)
                        f.seek(f.tell()-1)
                    else:
                        f.write(c)
        return len(text)
    def flush(self):
        self.stream1.flush()
        self.stream2.flush()

class LogOutput(object):
    """
    Copy output sent to stdout or stderr to a log file in the specified
    directory. Takes no action if directory is None. Log file is named based on
    the current date and time. Directory will be created if needed, and file
    will have microseconds added to the name if needed to avoid overwriting
    existing any existing file.

    TODO:
    - make this thread-aware (register and lookup an output stream for this
      particular thread),
    - accept model as argument instead of logs_dir and get the file name from
      model.options or else create a name as shown here
    - allow nesting (requesting same log file that is already open), so we can
      wrap the body of solve.main but also wrap solve.solve, solve.presolve,
      etc., so we can
    - make sure it appends to existing files rather than replacing
    - wrap all our API code (solve.main, solve.iterate, solve.solve,
      model.pre_solve, model.post_solve, model.__init__, etc.) with
      LogOutput(model) so that all log messages for one model will go to the
      same file, even if multiple models are processed at the same time in
      different threads or sequentially in the same thread.
    - Alternatively, treat logging as app-specific rather than model-specific,
      so switch solve or switch solve-scenarios identifies the active log file
      and log level when it first starts, and sticks with that until it exits.
    - Once all modules use loggers instead of print, it may be possible to have
      this work by creating file handlers for the root logger, each of which has
      a filter that only accepts messages if the current thread matches the
      thread that created that logger (i.e., only accepts messages from the
      thread that created it). Then the handler is removed when the block
      finishes.

    Note: Python/pyomo holds logging info on a singleton basis (in the logging module
    for each pyomo module name), so we either need to
        (1) patch the logging module to have/lookup different loggers per
            thread, probably based on thread ID, then wrap our API with a logger
            configuration (or stdout capture), so every operation on a particular
            model is wrapped with logger configuration for that model; or
        (2) treat logging as an application setting (either switch solve or
            switch solve-scenarios), not a model setting, so we just start
            logging/capture at startup and continue till the app exits; or
        (3) like (1) but with locks on our API to prevent multithreading, so we
            don't have to patch the logging module, just reconfigure the root logger
            at the start of each function (but this precludes any multithreaded
            loading/running of Switch models). (preferred)
    """
    def __init__(self, logs_dir):
        self.logs_dir = logs_dir
    def __enter__(self):
        """ start copying output to log file """
        if self.logs_dir is not None:
            log_file_path = self.make_file_path()
            self.log_file = open(log_file_path, 'w', buffering=1)
            self.stdout = sys.stdout
            self.stderr = sys.stderr
            sys.stdout = TeeStream(sys.stdout, self.log_file)
            # sys.stderr = TeeStream(sys.stderr, self.log_file)
            print("logging output to " + str(log_file_path))
    def __exit__(self, type, value, traceback):
        """ restore original output streams and close log file """
        if self.logs_dir is not None:
            sys.stdout = self.stdout
            sys.stderr = self.stderr
            self.log_file.close()
    def make_file_path(self):
        """
        Create a log file on disk and return the file name (guaranteed unique).
        When this function returns, the file exists but is empty and closed.
        """
        path = lambda format: os.path.join(
            self.logs_dir, datetime.datetime.now().strftime(format) + ".log"
        )
        # make sure logs directory exists
        if not os.path.exists(self.logs_dir):
            os.makedirs(self.logs_dir)
        file_path = path('%Y-%m-%d_%H-%M-%S')
        while True:
            try:
                f = os.open(file_path, os.O_CREAT | os.O_EXCL)
                # succeeded
                os.close(f)
                break
            except FileExistsError:
                # try again with microseconds in name and a little delay
                file_path = path('%Y-%m-%d_%H-%M-%S.%f')
        return file_path

class TimingLineCounterStream(object):
    """
    Virtual stream that intercepts lines like '0 seconds to construct  Set
    PERIODS; 1 index total\n' sent from Pyomo to stdout and turns them into a
    count of constructed components. Designed for use with
    model.create_instance(report_timing=True)
    """
    match_line = re.compile(r'^[ ]*[0-9.]+ seconds to construct .* total\n$')

    def __init__(self, orig_stream, model):
        self.orig_stream = orig_stream
        self.model = model
        self.last_message = ''
        self.components_completed = 0
    def __getattr__(self, *args, **kwargs):
        """
        Provide orig_stream attributes when attributes are requested for this class.
        This supports code that assumes sys.stdout is an object with its own
        methods, etc.
        """
        return getattr(self.orig_stream, *args, **kwargs)
    def write(self, text):
        if self.last_message and self.orig_stream.isatty():
                # Remove previous progress message; also overwrite with spaces
                # in case it's at end of a line. We skip this when not in a
                # terminal, e.g., in an IPython kernel.
                self.orig_stream.write(''.join(
                    c * len(self.last_message) for c in '\b \b'
                ))
        if self.match_line.match(text):
            self.components_completed += 1

            # report on progress
            # Note: the attached model is the abstract version, not the
            # instance currently being constructed, so we have no way to know
            # if components are added or deleted during construction. So we just
            # use the original component count and update if we overshoot.
            self.last_message = '{} of {} components constructed{}'.format(
                self.components_completed,
                max(self.components_completed, len(self.model._decl_order)),
                '' if self.orig_stream.isatty() else '\n'
            )
        else:
            # normal text, not a timing report
            self.orig_stream.write(text)
        # write the current message (possibly repeating below some normal text)
        self.orig_stream.write(self.last_message)

        return len(text)

class TimingLineCounter(object):
    """
    Intercept lines like '0 seconds to construct Set PERIODS; 1 index total'
    sent to stdout and turn them into a percentage count, relative to size of
    the passed model. Designed for use with model.create_instance(report_timing=True)
    """
    def __init__(self, model):
        self.model = model
    def __enter__(self):
        self.stdout = sys.stdout
        sys.stdout = TimingLineCounterStream(sys.stdout, self.model)
    def __exit__(self, type, value, traceback):
        if getattr(sys.stdout, 'last_message', ''):
            # add newline after last status message
            sys.stdout.last_message = ''
            print("")
        sys.stdout = self.stdout

# test_model = lambda: None
# test_model._decl_order = [1, 2, 3]
# lines = """Hello world!
#         0.01 seconds to construct Constraint Force_LNG_Tier; 260 indices total
# Blocking activation of tier ('Hawaii_LNG', 2045, 'container_25').
#         0.19 seconds to construct Set LNG_GEN_TIMEPOINTS; 1 index total
# Here's another message!
#         0.19 seconds to construct Set LNG_GEN_TIMEPOINTS; 1 index total"""
# with CountTimingLines(test_model):
#     for line in lines.split('\n'):
#         print(line)
#         time.sleep(0.5)

def iteritems(obj):
    """ Iterator of key, value pairs for obj;
    equivalent to obj.items() on Python 3+ and obj.iteritems() on Python 2 """
    try:
        return obj.iteritems()
    except AttributeError: # Python 3+
        return obj.items()

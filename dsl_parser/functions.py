########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

import pkg_resources
import abc

from dsl_parser import (constants,
                        exceptions,
                        scan)

SELF = 'SELF'
SOURCE = 'SOURCE'
TARGET = 'TARGET'
AVAILABLE_NODE_TARGETS = [SELF, SOURCE, TARGET]

TEMPLATE_FUNCTIONS = {}


def register(fn=None, name=None):
    if fn is None:
        def partial(_fn):
            return register(_fn, name=name)
        return partial
    TEMPLATE_FUNCTIONS[name] = fn
    fn.name = name
    return fn


def unregister(name):
    if name in TEMPLATE_FUNCTIONS:
        del TEMPLATE_FUNCTIONS[name]


def _register_entry_point_functions():
    for entry_point in pkg_resources.iter_entry_points(
            group='cloudify.tosca.ext.functions'):
        register(fn=entry_point.load(), name=entry_point.name)


def _is_function(value):
    """Does the value represent a template function call?"""
    # functions use the syntax {function_name: args}, so let's look for
    # dicts of length 1 where the only key was registered as a function
    return isinstance(value, dict) and len(value) == 1 \
        and list(value.keys())[0] in TEMPLATE_FUNCTIONS


def _convert_attribute_list_to_python_syntax_string(attr_list):
    """This converts the given attribute list to Python syntax. For example,
    calling this function with ['obj1', 'attr1', 7, 'attr2'] will output
    obj1.attr1[7].attr2.

    :param attr_list: the requested attributes/indices list.
    :return: string representing the requested attributes in Python syntax.
    """
    if not attr_list:
        return ''
    s = str(attr_list[0])
    for attr in attr_list[1:]:
        if isinstance(attr, int):
            s += '[{0}]'.format(attr)
        else:
            s += '.{0}'.format(attr)
    return s


_register_entry_point_functions()


class RuntimeEvaluationStorage(object):

    def __init__(self,
                 get_node_instances_method,
                 get_node_instance_method,
                 get_node_method,
                 get_secret_method,
                 get_capability_method):
        self._get_node_instances_method = get_node_instances_method
        self._get_node_instance_method = get_node_instance_method
        self._get_node_method = get_node_method
        self._get_secret_method = get_secret_method
        self._get_capability_method = get_capability_method

        self._node_to_node_instances = {}
        self._node_instances = {}
        self._nodes = {}
        self._secrets = {}
        self._capabilities = {}

    def get_node_instances(self, node_id):
        if node_id not in self._node_to_node_instances:
            node_instances = self._get_node_instances_method(node_id)
            self._node_to_node_instances[node_id] = node_instances
            for node_instance in node_instances:
                self._node_instances[node_instance.id] = node_instance
        return self._node_to_node_instances[node_id]

    def get_node_instance(self, node_instance_id):
        if node_instance_id not in self._node_instances:
            node_instance = self._get_node_instance_method(node_instance_id)
            self._node_instances[node_instance_id] = node_instance
        return self._node_instances[node_instance_id]

    def get_node(self, node_id):
        if node_id not in self._nodes:
            node = self._get_node_method(node_id)
            self._nodes[node_id] = node
        return self._nodes[node_id]

    def get_secret(self, secret_key):
        if secret_key not in self._secrets:
            secret = self._get_secret_method(secret_key)
            self._secrets[secret_key] = secret.value
        return self._secrets[secret_key]

    def get_capability(self, capability_path):
        capability_id = _convert_attribute_list_to_python_syntax_string(
            capability_path)
        if capability_id not in self._capabilities:
            capability = self._get_capability_method(capability_path)
            self._capabilities[capability_id] = capability
        return self._capabilities[capability_id]


class Function(object):

    __metaclass__ = abc.ABCMeta
    name = 'function'

    def __init__(self, args, scope=None, context=None, path=None, raw=None):
        self.scope = scope
        self.context = context
        self.path = path
        self.raw = raw
        self.parse_args(args)

    @abc.abstractmethod
    def parse_args(self, args):
        pass

    @abc.abstractmethod
    def validate(self, plan):
        pass

    @abc.abstractmethod
    def evaluate(self, plan):
        pass

    @abc.abstractmethod
    def evaluate_runtime(self, storage):
        pass


@register(name='get_input')
class GetInput(Function):

    def __init__(self, args, **kwargs):
        self.input_value = None
        super(GetInput, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, basestring) \
                and not (isinstance(args, list) and len(args) >= 1):
            raise ValueError(
                "Illegal argument(s) passed to {0} function. Expected either"
                "a string or a list [input_name [, key1/index1 [,...]]] but "
                "got {1}".format(self.name, args))
        self.input_value = args

    def validate(self, plan):
        input_value = self.input_value[0] \
            if isinstance(self.input_value, list) else self.input_value
        if input_value not in plan.inputs:
            raise exceptions.UnknownInputError(
                "get_input function references an "
                "unknown input '{0}'.".format(input_value))

    def evaluate(self, plan):
        if isinstance(self.input_value, list):
            return self._get_input_attribute(plan.inputs[self.input_value[0]])
        return plan.inputs[self.input_value]

    def _get_input_attribute(self, root):
        value = root
        for index, attr in enumerate(self.input_value[1:]):
            if isinstance(value, dict):
                if attr not in value:
                    raise exceptions.InputEvaluationError(
                        "Input attribute '{0}' of '{1}', "
                        "doesn't exist.".format(
                            attr,
                            _convert_attribute_list_to_python_syntax_string(
                                self.input_value[:index + 1])))

                value = value[attr]
            elif isinstance(value, list):
                try:
                    value = value[attr]
                except TypeError:
                    raise exceptions.InputEvaluationError(
                        "Item in index {0} in the get_input arguments list "
                        "'{1}' is expected to be an int but got {2}.".format(
                            index, self.input_value, type(attr).__name__))
                except IndexError:
                    raise exceptions.InputEvaluationError(
                        "List size of '{0}' is {1} but index {2} is "
                        "retrieved.".format(
                            _convert_attribute_list_to_python_syntax_string(
                                self.input_value[:index + 1]),
                            len(value),
                            attr))
            else:
                raise exceptions.FunctionEvaluationError(
                    self.name,
                    "Object {0} has no attribute {1}".format(
                        _convert_attribute_list_to_python_syntax_string(
                            self.input_value[:index + 1]), attr))
        return value

    def evaluate_runtime(self, storage):
        raise RuntimeError('runtime evaluation for {0} is not supported'
                           .format(self.name))


@register(name='get_property')
class GetProperty(Function):

    def __init__(self, args, **kwargs):
        self.node_name = None
        self.property_path = None
        super(GetProperty, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, list) or len(args) < 2:
            raise ValueError(
                'Illegal arguments passed to {0} function. Expected: '
                '<node_name, property_name [, nested-property-1, ... ]> but '
                'got: {1}.'.format(self.name, args))
        self.node_name = args[0]
        self.property_path = args[1:]

    def validate(self, plan):
        self.evaluate(plan)

    def get_node_template(self, plan):
        if self.node_name == SELF:
            if self.scope != scan.NODE_TEMPLATE_SCOPE:
                raise ValueError(
                    '{0} can only be used in a context of node template but '
                    'appears in {1}.'.format(SELF, self.scope))
            node = self.context
        elif self.node_name in [SOURCE, TARGET]:
            if self.scope != scan.NODE_TEMPLATE_RELATIONSHIP_SCOPE:
                raise ValueError(
                    '{0} can only be used within a relationship but is used '
                    'in {1}'.format(self.node_name, self.path))
            if self.node_name == SOURCE:
                node = self.context['node_template']
            else:
                target_node = self.context['relationship']['target_id']
                node = [
                    x for x in plan.node_templates
                    if x['name'] == target_node][0]
        else:
            found = [
                x for x in plan.node_templates if self.node_name == x['id']]
            if len(found) == 0:
                raise KeyError(
                    "{0} function node reference '{1}' does not exist.".format(
                        self.name, self.node_name))
            node = found[0]
        self._get_property_value(node)
        return node

    def _get_property_value(self, node_template):
        return _get_property_value(node_template['name'],
                                   node_template['properties'],
                                   self.property_path,
                                   self.path)

    def evaluate(self, plan):
        return self._get_property_value(self.get_node_template(plan))

    def evaluate_runtime(self, storage):
        raise RuntimeError('runtime evaluation for {0} is not supported'
                           .format(self.name))


@register(name='get_attribute')
class GetAttribute(Function):

    def __init__(self, args, **kwargs):
        self.node_name = None
        self.attribute_path = None
        super(GetAttribute, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, list) or len(args) < 2:
            raise ValueError(
                'Illegal arguments passed to {0} function. '
                'Expected: <node_name, attribute_name [, nested-attr-1, ...]>'
                'but got: {1}.'.format(self.name, args))
        self.node_name = args[0]
        self.attribute_path = args[1:]

    def validate(self, plan):
        if self.scope == scan.OUTPUTS_SCOPE and self.node_name in [SELF,
                                                                   SOURCE,
                                                                   TARGET]:
            raise ValueError('{0} cannot be used with {1} function in '
                             '{2}.'.format(self.node_name,
                                           self.name,
                                           self.path))
        if self.scope == scan.NODE_TEMPLATE_SCOPE and \
                self.node_name in [SOURCE, TARGET]:
            raise ValueError('{0} cannot be used with {1} function in '
                             '{2}.'.format(self.node_name,
                                           self.name,
                                           self.path))
        if self.scope == scan.NODE_TEMPLATE_RELATIONSHIP_SCOPE and \
                self.node_name == SELF:
            raise ValueError('{0} cannot be used with {1} function in '
                             '{2}.'.format(self.node_name,
                                           self.name,
                                           self.path))
        if self.node_name not in [SELF, SOURCE, TARGET]:
            found = [
                x for x in plan.node_templates if self.node_name == x['id']]
            if not found:
                raise KeyError(
                    "{0} function node reference '{1}' does not exist.".format(
                        self.name, self.node_name))

    def evaluate(self, plan):
        if 'operation' in self.context:
            self.context['operation']['has_intrinsic_functions'] = True
        return self.raw

    def evaluate_runtime(self, storage):
        if self.node_name == SELF:
            node_instance_id = self.context.get('self')
            self._validate_ref(node_instance_id, SELF)
            node_instance = storage.get_node_instance(node_instance_id)
        elif self.node_name == SOURCE:
            node_instance_id = self.context.get('source')
            self._validate_ref(node_instance_id, SOURCE)
            node_instance = storage.get_node_instance(node_instance_id)
        elif self.node_name == TARGET:
            node_instance_id = self.context.get('target')
            self._validate_ref(node_instance_id, TARGET)
            node_instance = storage.get_node_instance(node_instance_id)
        else:
            try:
                node_instance = self._resolve_node_instance_by_name(storage)
                node_instance_id = node_instance.id
            except exceptions.FunctionEvaluationError as e:
                # Only in outputs scope we allow to continue when an error
                # occurred
                if not self.context.get('evaluate_outputs'):
                    raise
                return '<ERROR: {0}>'.format(e.message)

        value = _get_property_value(node_instance.node_id,
                                    node_instance.runtime_properties,
                                    self.attribute_path,
                                    self.path,
                                    raise_if_not_found=False)
        # attribute not found in instance runtime properties
        if value is None:
            # special case for { get_attribute: [..., node_instance_id] }
            # returns the node-instance-id
            if len(self.attribute_path) == 1\
                    and self.attribute_path[0] == 'node_instance_id':
                value = node_instance_id
        # still nothing? look in node properties
        if value is None:
            node = storage.get_node(node_instance.node_id)
            value = _get_property_value(node.id,
                                        node.properties,
                                        self.attribute_path,
                                        self.path,
                                        raise_if_not_found=False)
        return value

    def _resolve_node_instance_by_name(self, storage):
        node_id = self.node_name
        node_instances = storage.get_node_instances(node_id)

        if len(node_instances) == 0:
            raise exceptions.FunctionEvaluationError(
                self.name,
                'Node {0} has no instances.'.format(self.node_name)
            )

        if len(node_instances) == 1:
            return node_instances[0]

        node_instance = self._try_resolve_node_instance_by_relationship(
            storage=storage,
            node_instances=node_instances)
        if node_instance:
            return node_instance

        node_instance = self._try_resolve_node_instance_by_scaling_group(
            storage=storage,
            node_instances=node_instances)
        if node_instance:
            return node_instance

        raise exceptions.FunctionEvaluationError(
            self.name,
            'More than one node instance found for node "{0}". Cannot '
            'resolve a node instance unambiguously.'
            .format(self.node_name))

    def _try_resolve_node_instance_by_relationship(
            self,
            storage,
            node_instances):
        self_instance_id = self.context.get('self')
        if not self_instance_id:
            return None
        self_instance = storage.get_node_instance(self_instance_id)
        self_instance_relationships = self_instance.relationships or []
        node_instances_target_ids = set()
        for relationship in self_instance_relationships:
            if relationship['target_name'] == self.node_name:
                node_instances_target_ids.add(relationship['target_id'])
        if len(node_instances_target_ids) != 1:
            return None
        node_instance_target_id = node_instances_target_ids.pop()
        for node_instance in node_instances:
            if node_instance.id == node_instance_target_id:
                return node_instance
        else:
            raise RuntimeError('Illegal state')

    def _try_resolve_node_instance_by_scaling_group(
            self,
            storage,
            node_instances):

        def _parent_instance(_instance):
            _node = storage.get_node(_instance.node_id)
            for relationship in _node.relationships or []:
                if (constants.CONTAINED_IN_REL_TYPE in
                        relationship['type_hierarchy']):
                    target_name = relationship['target_id']
                    target_id = [
                        r['target_id'] for r in _instance.relationships
                        if r['target_name'] == target_name][0]
                    return storage.get_node_instance(target_id)
            return None

        def _containing_groups(_instance):
            result = [g['name'] for g in _instance.scaling_groups or []]
            parent_instance = _parent_instance(_instance)
            if parent_instance:
                result += _containing_groups(parent_instance)
            return result

        def _minimal_shared_group(instance_a, instance_b):
            a_containing_groups = _containing_groups(instance_a)
            b_containing_groups = _containing_groups(instance_b)
            shared_groups = set(a_containing_groups) & set(b_containing_groups)
            if not shared_groups:
                return None
            for group in a_containing_groups:
                if group in shared_groups:
                    return group
            else:
                raise RuntimeError('Illegal state')

        def _group_instance(node_instance, group_name):
            for scaling_group in (node_instance.scaling_groups or []):
                if scaling_group['name'] == group_name:
                    return scaling_group['id']
            parent_instance = _parent_instance(node_instance)
            if not parent_instance:
                raise RuntimeError('Illegal state')
            return _group_instance(parent_instance, group_name)

        def _resolve_node_instance(context_instance_id):
            context_instance = storage.get_node_instance(context_instance_id)
            minimal_shared_group = _minimal_shared_group(context_instance,
                                                         node_instances[0])
            if not minimal_shared_group:
                return None

            context_group_instance = _group_instance(context_instance,
                                                     minimal_shared_group)
            result_node_instances = [
                i for i in node_instances if
                _group_instance(i, minimal_shared_group) ==
                context_group_instance]

            if len(result_node_instances) == 1:
                return result_node_instances[0]

            return None

        self_instance_id = self.context.get('self')
        source_instance_id = self.context.get('source')
        target_instance_id = self.context.get('target')
        if self_instance_id:
            return _resolve_node_instance(self_instance_id)
        elif source_instance_id:
            node_instance = _resolve_node_instance(source_instance_id)
            if node_instance:
                return node_instance
            node_instance = _resolve_node_instance(target_instance_id)
            if node_instance:
                return node_instance

        return None

    def _validate_ref(self, ref, ref_name):
        if not ref:
            raise exceptions.FunctionEvaluationError(
                self.name,
                '{0} is missing in request context in {1} for '
                'attribute {2}'.format(ref_name,
                                       self.path,
                                       self.attribute_path))


@register(name='get_secret')
class GetSecret(Function):
    def __init__(self, args, **kwargs):
        self.secret_id = None
        super(GetSecret, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, basestring):
            raise ValueError(
                "`get_secret` function argument should be a string. Instead "
                "it is a {0} with the value: {1}.".format(type(args), args))
        self.secret_id = args

    def validate(self, plan):
        pass

    def evaluate(self, plan):
        if 'operation' in self.context:
            self.context['operation']['has_intrinsic_functions'] = True
        return self.raw

    def evaluate_runtime(self, storage):
        return storage.get_secret(self.secret_id)


@register(name='get_capability')
class GetCapability(Function):
    def __init__(self, args, **kwargs):
        self.capability_path = None
        super(GetCapability, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, list):
            raise ValueError(
                "`get_capability` function argument should be a list. Instead "
                "it is a {0} with the value: {1}.".format(type(args), args))
        if len(args) < 2:
            raise ValueError(
                "`get_capability` function argument should be a list with 2 "
                "elements at least - [ deployment ID, capability ID "
                "[, key/index[, key/index [...]]] ]. Instead it is: "
                "{0}".format("[" + ','.join([str(a) for a in args]) + "]")
            )
        for arg_index in range(len(args)):
            if isinstance(args[arg_index], list):
                raise ValueError(
                    "`get_capability` function arguments can't be complex "
                    "values; only strings/ints/dicts are accepted. Instead, "
                    "the item with index {0} is {1} of type {2}".format(
                        arg_index, args[arg_index], type(args[arg_index])
                    )
                )

        self.capability_path = args

    def validate(self, plan):
        pass

    def evaluate(self, plan):
        if 'operation' in self.context:
            self.context['operation']['has_intrinsic_functions'] = True
        return self.raw

    def evaluate_runtime(self, storage):
        return storage.get_capability(self.capability_path)


@register(name='concat')
class Concat(Function):

    def __init__(self, args, **kwargs):
        self.separator = ''
        self.joined = args
        super(Concat, self).__init__(args, **kwargs)

    def parse_args(self, args):
        if not isinstance(args, list):
            raise ValueError(
                'Illegal arguments passed to {0} function. '
                'Expected: [arg1, arg2, ...]'
                'but got: {1}.'.format(self.name, args))

    def validate(self, plan):
        if plan.version.definitions_version < (1, 1):
            raise exceptions.FunctionEvaluationError(
                'Using {0} requires using dsl version 1_1 or '
                'greater, but found: {1} in {2}.'
                .format(self.name, plan.version, self.path))
        if self.scope not in [scan.NODE_TEMPLATE_SCOPE,
                              scan.NODE_TEMPLATE_RELATIONSHIP_SCOPE,
                              scan.OUTPUTS_SCOPE]:
            raise ValueError('{0} cannot be used in {1}.'
                             .format(self.name,
                                     self.path))

    def evaluate(self, plan):
        for joined_value in self.joined:
            if parse(joined_value) != joined_value:
                return self.raw
        return self.join()

    def evaluate_runtime(self, storage):
        return self.evaluate(plan=None)

    def join(self):
        str_join = [str(elem) for elem in self.joined]
        return self.separator.join(str_join)


def _get_property_value(node_name,
                        properties,
                        property_path,
                        context_path='',
                        raise_if_not_found=True):
    """Extracts a property's value according to the provided property path

    :param node_name: Node name the property belongs to (for logging).
    :param properties: Properties dict.
    :param property_path: Property path as list.
    :param context_path: Context path (for logging).
    :param raise_if_not_found: Whether to raise an error if property not found.
    :return: Property value.
    """
    def str_list(li):
        return [str(item) for item in li]

    value = properties
    for p in property_path:
        if _is_function(value):
            value = [value, p]
        elif isinstance(value, dict):
            if p not in value:
                if raise_if_not_found:
                    raise KeyError(
                        "Node template property '{0}.properties.{1}' "
                        "referenced from '{2}' doesn't exist.".format(
                            node_name, '.'.join(str_list(property_path)),
                            context_path))
                return None
            else:
                value = value[p]
        elif isinstance(value, list):
            try:
                value = value[p]
            except TypeError:
                raise TypeError(
                    "Node template property '{0}.properties.{1}' "
                    "referenced from '{2}' is expected {3} to be an int "
                    "but it is a {4}.".format(
                        node_name, '.'.join(str_list(property_path)),
                        context_path,
                        p, type(p).__name__))
            except IndexError:
                if raise_if_not_found:
                    raise IndexError(
                        "Node template property '{0}.properties.{1}' "
                        "referenced from '{2}' index is out of range. Got {3}"
                        " but list size is {4}.".format(
                            node_name, '.'.join(str_list(property_path)),
                            context_path, p, len(value)))
                return None
        else:
            if raise_if_not_found:
                raise KeyError(
                    "Node template property '{0}.properties.{1}' "
                    "referenced from '{2}' doesn't exist.".format(
                        node_name, '.'.join(str_list(property_path)),
                        context_path))
            return None

    return value


def parse(raw_function, scope=None, context=None, path=None):
    if _is_function(raw_function):
        func_name, func_args = raw_function.items()[0]
        return TEMPLATE_FUNCTIONS[func_name](func_args,
                                             scope=scope,
                                             context=context,
                                             path=path,
                                             raw=raw_function)
    return raw_function


def evaluate_functions(payload, context,
                       get_node_instances_method,
                       get_node_instance_method,
                       get_node_method,
                       get_secret_method,
                       get_capability_method):
    """Evaluate functions in payload.

    :param payload: The payload to evaluate.
    :param context: Context used during evaluation.
    :param get_node_instances_method: A method for getting node instances.
    :param get_node_instance_method: A method for getting a node instance.
    :param get_node_method: A method for getting a node.
    :param get_secret_method: A method for getting a secret.
    :param get_capability_method: A method for getting a capability.
    :return: payload.
    """
    handler = runtime_evaluation_handler(get_node_instances_method,
                                         get_node_instance_method,
                                         get_node_method,
                                         get_secret_method,
                                         get_capability_method)
    scan.scan_properties(payload,
                         handler,
                         scope=None,
                         context=context,
                         path='payload',
                         replace=True)
    return payload


def evaluate_capabilities(capabilities,
                          get_node_instances_method,
                          get_node_instance_method,
                          get_node_method,
                          get_secret_method,
                          get_capability_method):
    """Evaluates capabilities definition containing intrinsic functions.

    :param capabilities: The dict of capabilities to evaluate
    :param get_node_instances_method: A method for getting node instances.
    :param get_node_instance_method: A method for getting a node instance.
    :param get_node_method: A method for getting a node.
    :param get_secret_method: A method for getting a secret.
    :param get_capability_method: A method for getting a capability.
    :return: Capabilities dict.
    """
    capabilities = {k: v['value'] for k, v in capabilities.items()}
    return evaluate_functions(
        payload=capabilities,
        context={},
        get_node_instances_method=get_node_instances_method,
        get_node_instance_method=get_node_instance_method,
        get_node_method=get_node_method,
        get_secret_method=get_secret_method,
        get_capability_method=get_capability_method)


def evaluate_outputs(outputs_def,
                     get_node_instances_method,
                     get_node_instance_method,
                     get_node_method,
                     get_secret_method,
                     get_capability_method):
    """Evaluates an outputs definition containing intrinsic functions.

    :param outputs_def: Outputs definition.
    :param get_node_instances_method: A method for getting node instances.
    :param get_node_instance_method: A method for getting a node instance.
    :param get_node_method: A method for getting a node.
    :param get_secret_method: A method for getting a secret.
    :param get_capability_method: A method for getting a capability.
    :return: Outputs dict.
    """
    outputs = dict((k, v['value']) for k, v in outputs_def.iteritems())
    return evaluate_functions(
        payload=outputs,
        context={'evaluate_outputs': True},
        get_node_instances_method=get_node_instances_method,
        get_node_instance_method=get_node_instance_method,
        get_node_method=get_node_method,
        get_secret_method=get_secret_method,
        get_capability_method=get_capability_method)


def _handler(evaluator, **evaluator_kwargs):
    def handler(v, scope, context, path):
        evaluated_value = v
        scanned = False
        while True:
            func = parse(evaluated_value,
                         scope=scope,
                         context=context,
                         path=path)
            if not isinstance(func, Function):
                break
            previous_evaluated_value = evaluated_value
            evaluated_value = getattr(func, evaluator)(**evaluator_kwargs)
            if scanned and previous_evaluated_value == evaluated_value:
                break
            scan.scan_properties(evaluated_value,
                                 handler,
                                 scope=scope,
                                 context=context,
                                 path=path,
                                 replace=True)
            scanned = True
        return evaluated_value
    return handler


def plan_evaluation_handler(plan):
    return _handler('evaluate', plan=plan)


def runtime_evaluation_handler(get_node_instances_method,
                               get_node_instance_method,
                               get_node_method,
                               get_secret_method,
                               get_capability_method):
    return _handler('evaluate_runtime',
                    storage=RuntimeEvaluationStorage(
                        get_node_instances_method=get_node_instances_method,
                        get_node_instance_method=get_node_instance_method,
                        get_node_method=get_node_method,
                        get_secret_method=get_secret_method,
                        get_capability_method=get_capability_method))


def validate_functions(plan):
    get_property_functions = []

    def handler(v, scope, context, path):
        _func = parse(v, scope=scope, context=context, path=path)
        if isinstance(_func, Function):
            _func.validate(plan)
        if isinstance(_func, GetProperty):
            get_property_functions.append(_func)
            return _func
        return v

    # Replace all get_property functions with their instance representation
    scan.scan_service_template(plan, handler, replace=True)

    if not get_property_functions:
        return

    # Validate there are no circular get_property calls
    for func in get_property_functions:
        property_path = [str(prop) for prop in func.property_path]
        visited_functions = ['{0}.{1}'.format(
            func.get_node_template(plan)['name'],
            constants.FUNCTION_NAME_PATH_SEPARATOR.join(property_path))]

        def validate_no_circular_get_property(*args):
            r = args[0]
            if isinstance(r, GetProperty):
                func_id = '{0}.{1}'.format(
                    r.get_node_template(plan)['name'],
                    constants.FUNCTION_NAME_PATH_SEPARATOR.join(
                        r.property_path))
                if func_id in visited_functions:
                    visited_functions.append(func_id)
                    error_output = [
                        x.replace(constants.FUNCTION_NAME_PATH_SEPARATOR, ',')
                        for x in visited_functions
                    ]
                    raise RuntimeError(
                        'Circular get_property function call detected: '
                        '{0}'.format(' -> '.join(error_output)))
                visited_functions.append(func_id)
                r = r.evaluate(plan)
                validate_no_circular_get_property(r)
            else:
                scan.scan_properties(
                    r, validate_no_circular_get_property, recursive=False)

        result = func.evaluate(plan)
        validate_no_circular_get_property(result)

    def replace_with_raw_function(*args):
        if isinstance(args[0], GetProperty):
            return args[0].raw
        return args[0]

    # Change previously replaced get_property instances with raw values
    scan.scan_service_template(plan, replace_with_raw_function, replace=True)


def get_nested_attribute_value_of_capability(root, path):
    value = root
    for index, attr in enumerate(path[2:]):
        if isinstance(value, dict):
            if attr not in value:
                raise exceptions.FunctionEvaluationError(
                    "Attribute '{0}' doesn't exist in '{1}' "
                    "in deployment '{2}'.".format(
                        attr,
                        _convert_attribute_list_to_python_syntax_string(
                            path[1:index + 2]),
                        path[0]
                    )
                )

            value = value[attr]
        elif isinstance(value, list):
            try:
                value = value[attr]
            except TypeError:
                raise exceptions.FunctionEvaluationError(
                    "Item in index '{0}' in the get_capability arguments "
                    "list '{1}' is expected to be an int "
                    "but got {2}.".format(
                        index + 2, path, type(attr).__name__))
            except IndexError:
                raise exceptions.FunctionEvaluationError(
                    "List size of '{0}' is {1}, in "
                    "deployment '{2}', but index {3} is "
                    "retrieved.".format(
                        _convert_attribute_list_to_python_syntax_string(
                            path[1:index + 2]),
                        len(value),
                        path[0],
                        attr))
        else:
            raise exceptions.FunctionEvaluationError(
                "Object {0}, in capability '{1}' in deployment '{2}', "
                "has no attribute {3}".format(
                    _convert_attribute_list_to_python_syntax_string(
                        path[2:index + 2]),
                    path[1],
                    path[0],
                    attr))
    return {'value': value}

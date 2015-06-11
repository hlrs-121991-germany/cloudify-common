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

NODE_TEMPLATES = 'node_templates'
IMPORTS = 'imports'
NODE_TYPES = 'node_types'
PLUGINS = 'plugins'
INTERFACES = 'interfaces'
SOURCE_INTERFACES = 'source_interfaces'
TARGET_INTERFACES = 'target_interfaces'
WORKFLOWS = 'workflows'
RELATIONSHIPS = 'relationships'
PROPERTIES = 'properties'
PARAMETERS = 'parameters'
TYPE_HIERARCHY = 'type_hierarchy'
POLICY_TRIGGERS = 'policy_triggers'
POLICY_TYPES = 'policy_types'
GROUPS = 'groups'
INPUTS = 'inputs'
OUTPUTS = 'outputs'

HOST_TYPE = 'cloudify.nodes.Compute'
DEPENDS_ON_REL_TYPE = 'cloudify.relationships.depends_on'
CONTAINED_IN_REL_TYPE = 'cloudify.relationships.contained_in'
CONNECTED_TO_REL_TYPE = 'cloudify.relationships.connected_to'

CENTRAL_DEPLOYMENT_AGENT = 'central_deployment_agent'
HOST_AGENT = 'host_agent'
PLUGIN_EXECUTOR_KEY = 'executor'
PLUGIN_SOURCE_KEY = 'source'
PLUGIN_INSTALL_KEY = 'install'
PLUGIN_INSTALL_ARGUMENTS_KEY = 'install_arguments'
PLUGIN_NAME_KEY = 'name'
PLUGINS_TO_INSTALL = 'plugins_to_install'
DEPLOYMENT_PLUGINS_TO_INSTALL = 'deployment_plugins_to_install'
WORKFLOW_PLUGINS_TO_INSTALL = 'workflow_plugins_to_install'
VERSION = 'version'

SCRIPT_PLUGIN_NAME = 'script'
SCRIPT_PLUGIN_RUN_TASK = 'script_runner.tasks.run'
SCRIPT_PLUGIN_EXECUTE_WORKFLOW_TASK = 'script_runner.tasks.execute_workflow'
SCRIPT_PATH_PROPERTY = 'script_path'

FUNCTION_NAME_PATH_SEPARATOR = '__sep__'

NODES = 'nodes'
NODE_INSTANCES = 'node_instances'

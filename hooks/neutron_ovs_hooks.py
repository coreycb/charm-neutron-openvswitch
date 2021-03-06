#!/usr/bin/python
#
# Copyright 2016 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys

from copy import deepcopy

from charmhelpers.contrib.openstack.utils import (
    config_value_changed,
    git_install_requested,
    os_requires_version,
    pausable_restart_on_change as restart_on_change,
)

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    log,
    relation_set,
    relation_ids,
)

from neutron_ovs_utils import (
    DHCP_PACKAGES,
    DVR_PACKAGES,
    METADATA_PACKAGES,
    configure_ovs,
    git_install,
    get_topics,
    get_shared_secret,
    register_configs,
    restart_map,
    use_dvr,
    enable_nova_metadata,
    enable_local_dhcp,
    install_packages,
    purge_packages,
    assess_status,
)

hooks = Hooks()
CONFIGS = register_configs()


@hooks.hook()
def install():
    install_packages()
    git_install(config('openstack-origin-git'))


@hooks.hook('neutron-plugin-relation-changed')
@hooks.hook('config-changed')
@restart_on_change(restart_map())
def config_changed():
    install_packages()
    if git_install_requested():
        if config_value_changed('openstack-origin-git'):
            git_install(config('openstack-origin-git'))

    configure_ovs()
    CONFIGS.write_all()
    for rid in relation_ids('zeromq-configuration'):
        zeromq_configuration_relation_joined(rid)
    for rid in relation_ids('neutron-plugin'):
        neutron_plugin_joined(relation_id=rid)


@hooks.hook('neutron-plugin-api-relation-changed')
@restart_on_change(restart_map())
def neutron_plugin_api_changed():
    if use_dvr():
        install_packages()
    else:
        purge_packages(DVR_PACKAGES)
    configure_ovs()
    CONFIGS.write_all()
    # If dvr setting has changed, need to pass that on
    for rid in relation_ids('neutron-plugin'):
        neutron_plugin_joined(relation_id=rid)


@hooks.hook('neutron-plugin-relation-joined')
def neutron_plugin_joined(relation_id=None):
    if enable_local_dhcp():
        install_packages()
    else:
        pkgs = deepcopy(DHCP_PACKAGES)
        # NOTE: only purge metadata packages if dvr is not
        #       in use as this will remove the l3 agent
        #       see https://pad.lv/1515008
        if not use_dvr():
            pkgs.extend(METADATA_PACKAGES)
        purge_packages(pkgs)
    secret = get_shared_secret() if enable_nova_metadata() else None
    rel_data = {
        'metadata-shared-secret': secret,
    }
    relation_set(relation_id=relation_id, **rel_data)


@hooks.hook('amqp-relation-joined')
def amqp_joined(relation_id=None):
    relation_set(relation_id=relation_id,
                 username=config('rabbit-user'),
                 vhost=config('rabbit-vhost'))


@hooks.hook('amqp-relation-changed')
@hooks.hook('amqp-relation-departed')
@restart_on_change(restart_map())
def amqp_changed():
    if 'amqp' not in CONFIGS.complete_contexts():
        log('amqp relation incomplete. Peer not ready?')
        return
    CONFIGS.write_all()


@hooks.hook('zeromq-configuration-relation-joined')
@os_requires_version('kilo', 'neutron-common')
def zeromq_configuration_relation_joined(relid=None):
    relation_set(relation_id=relid,
                 topics=" ".join(get_topics()),
                 users="neutron")


@hooks.hook('zeromq-configuration-relation-changed')
@restart_on_change(restart_map(), stopstart=True)
def zeromq_configuration_relation_changed():
    CONFIGS.write_all()


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e))
    assess_status(CONFIGS)


if __name__ == '__main__':
    main()

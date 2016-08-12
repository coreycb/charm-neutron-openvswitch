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

import os
import shutil
from itertools import chain
import subprocess

from charmhelpers.contrib.openstack.neutron import neutron_plugin_attribute
from copy import deepcopy

from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.openstack.utils import (
    git_clone_and_install,
    git_default_repos,
    git_generate_systemd_init_files,
    git_install_requested,
    git_pip_venv_dir,
    git_src_dir,
    pause_unit,
    resume_unit,
    make_assess_status_func,
    is_unit_paused_set,
)
from collections import OrderedDict
from charmhelpers.contrib.openstack.utils import (
    os_release,
)
import neutron_ovs_context
from charmhelpers.contrib.network.ovs import (
    add_bridge,
    add_bridge_port,
    full_restart,
)
from charmhelpers.core.hookenv import (
    charm_dir,
    config,
    status_set,
)
from charmhelpers.contrib.openstack.neutron import (
    parse_bridge_mappings,
    determine_dkms_package,
    headers_package,
)
from charmhelpers.contrib.openstack.context import (
    ExternalPortContext,
    DataPortContext,
)
from charmhelpers.core.host import (
    adduser,
    add_group,
    add_user_to_group,
    lsb_release,
    mkdir,
    service,
    service_restart,
    service_running,
    write_file,
)

from charmhelpers.core.templating import render

from charmhelpers.fetch import (
    apt_install,
    apt_purge,
    apt_update,
    filter_installed_packages,
)

# The interface is said to be satisfied if anyone of the interfaces in the
# list has a complete context.
# LY: Note the neutron-plugin is always present since that is the relation
#     with the principle and no data currently flows down from the principle
#     so there is no point in having it in REQUIRED_INTERFACES
REQUIRED_INTERFACES = {
    'messaging': ['amqp', 'zeromq-configuration'],
}

BASE_GIT_PACKAGES = [
    'ipset',
    'libffi-dev',
    'libssl-dev',
    'libxml2-dev',
    'libxslt1-dev',
    'libyaml-dev',
    'openstack-pkg-tools',
    'openvswitch-switch',
    'python-dev',
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
]

# ubuntu packages that should not be installed when deploying from git
GIT_PACKAGE_BLACKLIST = [
    'neutron-l3-agent',
    'neutron-metadata-agent',
    'neutron-server',
    'neutron-plugin-openvswitch',
    'neutron-plugin-openvswitch-agent',
    'neutron-openvswitch',
    'neutron-openvswitch-agent',
    'neutron-openvswitch-agent',
]

NOVA_CONF_DIR = "/etc/nova"
NEUTRON_DHCP_AGENT_CONF = "/etc/neutron/dhcp_agent.ini"
NEUTRON_CONF_DIR = "/etc/neutron"
NEUTRON_CONF = '%s/neutron.conf' % NEUTRON_CONF_DIR
NEUTRON_DEFAULT = '/etc/default/neutron-server'
NEUTRON_L3_AGENT_CONF = "/etc/neutron/l3_agent.ini"
NEUTRON_FWAAS_CONF = "/etc/neutron/fwaas_driver.ini"
ML2_CONF = '%s/plugins/ml2/ml2_conf.ini' % NEUTRON_CONF_DIR
OVS_CONF = '%s/plugins/ml2/openvswitch_agent.ini' % NEUTRON_CONF_DIR
EXT_PORT_CONF = '/etc/init/ext-port.conf'
NEUTRON_METADATA_AGENT_CONF = "/etc/neutron/metadata_agent.ini"
DVR_PACKAGES = ['neutron-l3-agent']
DHCP_PACKAGES = ['neutron-dhcp-agent']
METADATA_PACKAGES = ['neutron-metadata-agent']
PHY_NIC_MTU_CONF = '/etc/init/os-charm-phy-nic-mtu.conf'
TEMPLATES = 'templates/'
OVS_DEFAULT = '/etc/default/openvswitch-switch'
DPDK_INTERFACES = '/etc/dpdk/interfaces'

BASE_RESOURCE_MAP = OrderedDict([
    (NEUTRON_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext(),
                     neutron_ovs_context.RemoteRestartContext(),
                     context.AMQPContext(ssl_dir=NEUTRON_CONF_DIR),
                     context.ZeroMQContext(),
                     context.NotificationDriverContext()],
    }),
    (ML2_CONF, {
        'services': ['neutron-plugin-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_CONF, {
        'services': ['neutron-openvswitch-agent'],
        'contexts': [neutron_ovs_context.OVSPluginContext()],
    }),
    (OVS_DEFAULT, {
        'services': ['openvswitch-switch'],
        'contexts': [neutron_ovs_context.OVSDPDKDeviceContext()],
    }),
    (DPDK_INTERFACES, {
        'services': ['dpdk'],
        'contexts': [neutron_ovs_context.DPDKDeviceContext()],
    }),

    (PHY_NIC_MTU_CONF, {
        'services': ['os-charm-phy-nic-mtu'],
        'contexts': [context.PhyNICMTUContext()],
    }),
])
METADATA_RESOURCE_MAP = OrderedDict([
    (NEUTRON_METADATA_AGENT_CONF, {
        'services': ['neutron-metadata-agent'],
        'contexts': [neutron_ovs_context.SharedSecretContext(),
                     neutron_ovs_context.APIIdentityServiceContext()],
    }),
])
DHCP_RESOURCE_MAP = OrderedDict([
    (NEUTRON_DHCP_AGENT_CONF, {
        'services': ['neutron-dhcp-agent'],
        'contexts': [],
    }),
])
DVR_RESOURCE_MAP = OrderedDict([
    (NEUTRON_L3_AGENT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (NEUTRON_FWAAS_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [neutron_ovs_context.L3AgentContext()],
    }),
    (EXT_PORT_CONF, {
        'services': ['neutron-l3-agent'],
        'contexts': [context.ExternalPortContext()],
    }),
])

TEMPLATES = 'templates/'
INT_BRIDGE = "br-int"
EXT_BRIDGE = "br-ex"
DATA_BRIDGE = 'br-data'


def install_packages():
    status_set('maintenance', 'Installing apt packages')
    apt_update()
    # NOTE(jamespage): ensure early install of dkms related
    #                  dependencies for kernels which need
    #                  openvswitch via dkms (12.04).
    dkms_packages = determine_dkms_package()
    if dkms_packages:
        apt_install([headers_package()] + dkms_packages, fatal=True)
    apt_install(filter_installed_packages(determine_packages()),
                fatal=True)
    if use_dpdk():
        enable_ovs_dpdk()


def purge_packages(pkg_list):
    status_set('maintenance', 'Purging unused apt packages')
    purge_pkgs = []
    required_packages = determine_packages()
    for pkg in pkg_list:
        if pkg not in required_packages:
            purge_pkgs.append(pkg)
    if purge_pkgs:
        apt_purge(purge_pkgs, fatal=True)


def determine_packages():
    pkgs = []
    plugin_pkgs = neutron_plugin_attribute('ovs', 'packages', 'neutron')
    for plugin_pkg in plugin_pkgs:
        pkgs.extend(plugin_pkg)
    if use_dvr():
        pkgs.extend(DVR_PACKAGES)
    if enable_local_dhcp():
        pkgs.extend(DHCP_PACKAGES)
        pkgs.extend(METADATA_PACKAGES)

    if git_install_requested():
        pkgs.extend(BASE_GIT_PACKAGES)
        # don't include packages that will be installed from git
        for p in GIT_PACKAGE_BLACKLIST:
            if p in pkgs:
                pkgs.remove(p)

    release = os_release('neutron-common', base='icehouse')
    if release >= 'mitaka' and 'neutron-plugin-openvswitch-agent' in pkgs:
        pkgs.remove('neutron-plugin-openvswitch-agent')
        pkgs.append('neutron-openvswitch-agent')

    if use_dpdk():
        pkgs.append('openvswitch-switch-dpdk')

    return pkgs


def register_configs(release=None):
    release = release or os_release('neutron-common', base='icehouse')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def resource_map():
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    resource_map = deepcopy(BASE_RESOURCE_MAP)
    if use_dvr():
        resource_map.update(DVR_RESOURCE_MAP)
        resource_map.update(METADATA_RESOURCE_MAP)
        dvr_services = ['neutron-metadata-agent', 'neutron-l3-agent']
        resource_map[NEUTRON_CONF]['services'] += dvr_services
    if enable_local_dhcp():
        resource_map.update(METADATA_RESOURCE_MAP)
        resource_map.update(DHCP_RESOURCE_MAP)
        metadata_services = ['neutron-metadata-agent', 'neutron-dhcp-agent']
        resource_map[NEUTRON_CONF]['services'] += metadata_services
    # Remap any service names as required
    if os_release('neutron-common', base='icehouse') >= 'mitaka':
        # ml2_conf.ini -> openvswitch_agent.ini
        del resource_map[ML2_CONF]
        # drop of -plugin from service name
        resource_map[NEUTRON_CONF]['services'].remove(
            'neutron-plugin-openvswitch-agent'
        )
        resource_map[NEUTRON_CONF]['services'].append(
            'neutron-openvswitch-agent'
        )
        if not use_dpdk():
            # NOTE; /etc/default/openvswitch only used for
            #       DPDK configuration so drop if DPDK not
            #       in use
            del resource_map[OVS_DEFAULT]
            del resource_map[DPDK_INTERFACES]
    else:
        del resource_map[OVS_CONF]
        del resource_map[OVS_DEFAULT]
        del resource_map[DPDK_INTERFACES]
    return resource_map


def restart_map():
    '''
    Constructs a restart map based on charm config settings and relation
    state.
    '''
    return {k: v['services'] for k, v in resource_map().iteritems()}


def get_topics():
    topics = []
    topics.append('q-agent-notifier-port-update')
    topics.append('q-agent-notifier-network-delete')
    topics.append('q-agent-notifier-tunnel-update')
    topics.append('q-agent-notifier-security_group-update')
    topics.append('q-agent-notifier-dvr-update')
    if context.NeutronAPIContext()()['l2_population']:
        topics.append('q-agent-notifier-l2population-update')
    return topics


def services():
    """Returns a list of (unique) services associate with this charm
    Note that we drop the os-charm-phy-nic-mtu service as it's not an actual
    running service that we can check for.

    @returns [strings] - list of service names suitable for (re)start_service()
    """
    s_set = set(chain(*restart_map().values()))
    s_set.discard('os-charm-phy-nic-mtu')
    return list(s_set)


def determine_ports():
    """Assemble a list of API ports for services the charm is managing

    @returns [ports] - list of ports that the charm manages.
    """
    ports = []
    if use_dvr():
        ports.append(DVR_RESOURCE_MAP[EXT_PORT_CONF]["ext_port"])
    return ports


UPDATE_ALTERNATIVES = ['update-alternatives', '--set', 'ovs-vswitchd']
OVS_DPDK_BIN = '/usr/lib/openvswitch-switch-dpdk/ovs-vswitchd-dpdk'
OVS_DEFAULT_BIN = '/usr/lib/openvswitch-switch/ovs-vswitchd'


def enable_ovs_dpdk():
    '''Enables the DPDK variant of ovs-vswitchd and restarts it'''
    subprocess.check_call(UPDATE_ALTERNATIVES + [OVS_DPDK_BIN])
    if not is_unit_paused_set():
        service_restart('openvswitch-switch')


def configure_ovs():
    status_set('maintenance', 'Configuring ovs')
    if not service_running('openvswitch-switch'):
        full_restart()
    datapath_type = determine_datapath_type()
    add_bridge(INT_BRIDGE, datapath_type)
    add_bridge(EXT_BRIDGE, datapath_type)
    ext_port_ctx = None
    if use_dvr():
        ext_port_ctx = ExternalPortContext()()
    if ext_port_ctx and ext_port_ctx['ext_port']:
        add_bridge_port(EXT_BRIDGE, ext_port_ctx['ext_port'])

    if not use_dpdk():
        portmaps = DataPortContext()()
        bridgemaps = parse_bridge_mappings(config('bridge-mappings'))
        for br in bridgemaps.itervalues():
            add_bridge(br, datapath_type)
            if not portmaps:
                continue

            for port, _br in portmaps.iteritems():
                if _br == br:
                    add_bridge_port(br, port, promisc=True)
    else:
        # NOTE: when in dpdk mode, add based on pci bus order
        #       with type 'dpdk'
        dpdk_bridgemaps = neutron_ovs_context.resolve_dpdk_ports()
        device_index = 0
        for br in dpdk_bridgemaps.itervalues():
            add_bridge(br, datapath_type)
            dpdk_add_bridge_port(br, 'dpdk{}'.format(device_index),
                                 port_type='dpdk')
            device_index += 1

    # Ensure this runs so that mtu is applied to data-port interfaces if
    # provided.
    # NOTE(ajkavanagh) for pause/resume we don't gate this as it's not a
    # running service, but rather running a few commands.
    service_restart('os-charm-phy-nic-mtu')


def get_shared_secret():
    ctxt = neutron_ovs_context.SharedSecretContext()()
    if 'shared_secret' in ctxt:
        return ctxt['shared_secret']


def use_dvr():
    return context.NeutronAPIContext()()['enable_dvr']


def determine_datapath_type():
    '''
    Determine the ovs datapath type to use

    @returns string containing the datapath type
    '''
    if use_dpdk():
        return 'netdev'
    return 'system'


def use_dpdk():
    '''Determine whether DPDK should be used'''
    release = os_release('neutron-common', base='icehouse')
    if (release >= 'mitaka' and config('enable-dpdk')):
        return True
    return False


# TODO: update into charm-helpers to add port_type parameter
def dpdk_add_bridge_port(name, port, promisc=False, port_type=None):
    ''' Add a port to the named openvswitch bridge '''
    # log('Adding port {} to bridge {}'.format(port, name))
    cmd = ["ovs-vsctl", "--", "--may-exist", "add-port", name, port]
    if port_type:
        cmd += ['--', 'set', 'Interface', port,
                'type={}'.format(port_type)]
    subprocess.check_call(cmd)


def enable_nova_metadata():
    return use_dvr() or enable_local_dhcp()


def enable_local_dhcp():
    return config('enable-local-dhcp-and-metadata')


def git_install(projects_yaml):
    """Perform setup, and install git repos specified in yaml parameter."""
    status_set('maintenance', 'running git install')
    if git_install_requested():
        git_pre_install()
        projects_yaml = git_default_repos(projects_yaml)
        git_clone_and_install(projects_yaml, core_project='neutron')
        git_post_install(projects_yaml)


def git_pre_install():
    """Perform pre-install setup."""
    dirs = [
        '/var/lib/neutron',
        '/var/lib/neutron/lock',
        '/var/log/neutron',
    ]

    logs = [
        '/var/log/neutron/server.log',
    ]

    adduser('neutron', shell='/bin/bash', system_user=True)
    add_group('neutron', system_group=True)
    add_user_to_group('neutron', 'neutron')

    for d in dirs:
        mkdir(d, owner='neutron', group='neutron', perms=0755, force=False)

    for l in logs:
        write_file(l, '', owner='neutron', group='neutron', perms=0600)


def git_post_install(projects_yaml):
    """Perform post-install setup."""
    src_etc = os.path.join(git_src_dir(projects_yaml, 'neutron'), 'etc')
    configs = [
        {'src': src_etc,
         'dest': '/etc/neutron'},
        {'src': os.path.join(src_etc, 'neutron/plugins'),
         'dest': '/etc/neutron/plugins'},
        {'src': os.path.join(src_etc, 'neutron/rootwrap.d'),
         'dest': '/etc/neutron/rootwrap.d'},
    ]

    for c in configs:
        if os.path.exists(c['dest']):
            shutil.rmtree(c['dest'])
        shutil.copytree(c['src'], c['dest'])

    # NOTE(coreycb): Need to find better solution than bin symlinks.
    symlinks = [
        {'src': os.path.join(git_pip_venv_dir(projects_yaml),
                             'bin/neutron-rootwrap'),
         'link': '/usr/local/bin/neutron-rootwrap'},
    ]

    for s in symlinks:
        if os.path.lexists(s['link']):
            os.remove(s['link'])
        os.symlink(s['src'], s['link'])

    render('git/neutron_sudoers', '/etc/sudoers.d/neutron_sudoers', {},
           perms=0o440)

    bin_dir = os.path.join(git_pip_venv_dir(projects_yaml), 'bin')
    # Use systemd init units/scripts from ubuntu wily onward
    if lsb_release()['DISTRIB_RELEASE'] >= '15.10':
        templates_dir = os.path.join(charm_dir(), 'templates/git')
        daemons = ['neutron-openvswitch-agent', 'neutron-ovs-cleanup']
        for daemon in daemons:
            neutron_ovs_context = {
                'daemon_path': os.path.join(bin_dir, daemon),
            }
            filename = daemon
            if daemon == 'neutron-openvswitch-agent':
                if os_release('neutron-common') < 'mitaka':
                    filename = 'neutron-plugin-openvswitch-agent'
            template_file = 'git/{}.init.in.template'.format(filename)
            init_in_file = '{}.init.in'.format(filename)
            render(template_file, os.path.join(templates_dir, init_in_file),
                   neutron_ovs_context, perms=0o644)
        git_generate_systemd_init_files(templates_dir)

        for daemon in daemons:
            filename = daemon
            if daemon == 'neutron-openvswitch-agent':
                if os_release('neutron-common') < 'mitaka':
                    filename = 'neutron-plugin-openvswitch-agent'
            service('enable', filename)
    else:
        neutron_ovs_agent_context = {
            'service_description': 'Neutron OpenvSwitch Plugin Agent',
            'charm_name': 'neutron-openvswitch',
            'process_name': 'neutron-openvswitch-agent',
            'executable_name': os.path.join(bin_dir,
                                            'neutron-openvswitch-agent'),
            'cleanup_process_name': 'neutron-ovs-cleanup',
            'plugin_config': '/etc/neutron/plugins/ml2/ml2_conf.ini',
            'log_file': '/var/log/neutron/openvswitch-agent.log',
        }

        neutron_ovs_cleanup_context = {
            'service_description': 'Neutron OpenvSwitch Cleanup',
            'charm_name': 'neutron-openvswitch',
            'process_name': 'neutron-ovs-cleanup',
            'executable_name': os.path.join(bin_dir, 'neutron-ovs-cleanup'),
            'log_file': '/var/log/neutron/ovs-cleanup.log',
        }

        if os_release('neutron-common') < 'mitaka':
            render('git/upstart/neutron-plugin-openvswitch-agent.upstart',
                   '/etc/init/neutron-plugin-openvswitch-agent.conf',
                   neutron_ovs_agent_context, perms=0o644)
        else:
            render('git/upstart/neutron-plugin-openvswitch-agent.upstart',
                   '/etc/init/neutron-openvswitch-agent.conf',
                   neutron_ovs_agent_context, perms=0o644)
        render('git/upstart/neutron-ovs-cleanup.upstart',
               '/etc/init/neutron-ovs-cleanup.conf',
               neutron_ovs_cleanup_context, perms=0o644)

    if not is_unit_paused_set():
        service_restart('neutron-plugin-openvswitch-agent')


def assess_status(configs):
    """Assess status of current unit
    Decides what the state of the unit should be based on the current
    configuration.
    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    Note that required_interfaces is augmented with neutron-plugin-api if the
    nova_metadata is enabled.

    NOTE(ajkavanagh) ports are not checked due to race hazards with services
    that don't behave sychronously w.r.t their service scripts.  e.g.
    apache2.
    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    required_interfaces = REQUIRED_INTERFACES.copy()
    if enable_nova_metadata():
        required_interfaces['neutron-plugin-api'] = ['neutron-plugin-api']
    return make_assess_status_func(
        configs, required_interfaces,
        services=services(), ports=None)


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.
    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit
    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    f(assess_status_func(configs),
      services=services(),
      ports=None)

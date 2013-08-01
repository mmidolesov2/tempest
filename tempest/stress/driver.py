# Copyright 2013 Quanta Research Cambridge, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import logging
import multiprocessing
import signal
import time

from tempest import clients
from tempest.common import ssh
from tempest.common.utils.data_utils import rand_name
from tempest import exceptions
from tempest.openstack.common import importutils
from tempest.stress import cleanup

admin_manager = clients.AdminManager()

# setup logging to file
logging.basicConfig(
    format='%(asctime)s %(process)d %(name)-20s %(levelname)-8s %(message)s',
    datefmt='%m-%d %H:%M:%S',
    filename="stress.debug.log",
    filemode="w",
    level=logging.DEBUG,
)

# define a Handler which writes INFO messages or higher to the sys.stdout
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
# set a format which is simpler for console use
format_str = '%(asctime)s %(process)d %(name)-20s: %(levelname)-8s %(message)s'
_formatter = logging.Formatter(format_str)
# tell the handler to use this format
_console.setFormatter(_formatter)
# add the handler to the root logger
logger = logging.getLogger('tempest.stress')
logger.addHandler(_console)
processes = []


def do_ssh(command, host):
    username = admin_manager.config.stress.target_ssh_user
    key_filename = admin_manager.config.stress.target_private_key_path
    if not (username and key_filename):
        return None
    ssh_client = ssh.Client(host, username, key_filename=key_filename)
    try:
        return ssh_client.exec_command(command)
    except exceptions.SSHExecCommandFailed:
        return None


def _get_compute_nodes(controller):
    """
    Returns a list of active compute nodes. List is generated by running
    nova-manage on the controller.
    """
    nodes = []
    cmd = "nova-manage service list | grep ^nova-compute"
    output = do_ssh(cmd, controller)
    if not output:
        return nodes
    # For example: nova-compute xg11eth0 nova enabled :-) 2011-10-31 18:57:46
    # This is fragile but there is, at present, no other way to get this info.
    for line in output.split('\n'):
        words = line.split()
        if len(words) > 0 and words[4] == ":-)":
            nodes.append(words[1])
    return nodes


def _error_in_logs(logfiles, nodes):
    """
    Detect errors in the nova log files on the controller and compute nodes.
    """
    grep = 'egrep "ERROR|TRACE" %s' % logfiles
    for node in nodes:
        errors = do_ssh(grep, node)
        if not errors:
            return None
        if len(errors) > 0:
            logger.error('%s: %s' % (node, errors))
            return errors
    return None


def sigchld_handler(signal, frame):
    """
    Signal handler (only active if stop_on_error is True).
    """
    terminate_all_processes()


def terminate_all_processes():
    """
    Goes through the process list and terminates all child processes.
    """
    for process in processes:
        if process['process'].is_alive():
            try:
                process['process'].terminate()
            except Exception:
                pass
        process['process'].join()


def stress_openstack(tests, duration, max_runs=None, stop_on_error=False):
    """
    Workload driver. Executes an action function against a nova-cluster.
    """
    logfiles = admin_manager.config.stress.target_logfiles
    log_check_interval = int(admin_manager.config.stress.log_check_interval)
    if logfiles:
        controller = admin_manager.config.stress.target_controller
        computes = _get_compute_nodes(controller)
        for node in computes:
            do_ssh("rm -f %s" % logfiles, node)
    for test in tests:
        if test.get('use_admin', False):
            manager = admin_manager
        else:
            manager = clients.Manager()
        for p_number in xrange(test.get('threads', 1)):
            if test.get('use_isolated_tenants', False):
                username = rand_name("stress_user")
                tenant_name = rand_name("stress_tenant")
                password = "pass"
                identity_client = admin_manager.identity_client
                _, tenant = identity_client.create_tenant(name=tenant_name)
                identity_client.create_user(username,
                                            password,
                                            tenant['id'],
                                            "email")
                manager = clients.Manager(username=username,
                                          password="pass",
                                          tenant_name=tenant_name)

            test_obj = importutils.import_class(test['action'])
            test_run = test_obj(manager, logger, max_runs, stop_on_error)

            kwargs = test.get('kwargs', {})
            test_run.setUp(**dict(kwargs.iteritems()))

            logger.debug("calling Target Object %s" %
                         test_run.__class__.__name__)

            mp_manager = multiprocessing.Manager()
            shared_statistic = mp_manager.dict()
            shared_statistic['runs'] = 0
            shared_statistic['fails'] = 0

            p = multiprocessing.Process(target=test_run.execute,
                                        args=(shared_statistic,))

            process = {'process': p,
                       'p_number': p_number,
                       'action': test['action'],
                       'statistic': shared_statistic}

            processes.append(process)
            p.start()
    if stop_on_error:
        # NOTE(mkoderer): only the parent should register the handler
        signal.signal(signal.SIGCHLD, sigchld_handler)
    end_time = time.time() + duration
    had_errors = False
    while True:
        if max_runs is None:
            remaining = end_time - time.time()
            if remaining <= 0:
                break
        else:
            remaining = log_check_interval
            all_proc_term = True
            for process in processes:
                if process['process'].is_alive():
                    all_proc_term = False
                    break
            if all_proc_term:
                break

        time.sleep(min(remaining, log_check_interval))
        if stop_on_error:
            for process in processes:
                if process['statistic']['fails'] > 0:
                    break

        if not logfiles:
            continue
        errors = _error_in_logs(logfiles, computes)
        if errors:
            had_errors = True
            break

    terminate_all_processes()

    sum_fails = 0
    sum_runs = 0

    logger.info("Statistics (per process):")
    for process in processes:
        if process['statistic']['fails'] > 0:
            had_errors = True
        sum_runs += process['statistic']['runs']
        sum_fails += process['statistic']['fails']
        logger.info(" Process %d (%s): Run %d actions (%d failed)" %
                    (process['p_number'],
                     process['action'],
                     process['statistic']['runs'],
                     process['statistic']['fails']))
    logger.info("Summary:")
    logger.info("Run %d actions (%d failed)" %
                (sum_runs, sum_fails))

    if not had_errors:
        logger.info("cleaning up")
        cleanup.cleanup(logger)
    if had_errors:
        return 1
    else:
        return 0

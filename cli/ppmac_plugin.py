#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
:mod:`ppmac_plugin` -- Ppmac Core module
========================================

.. module:: ppmac_plugin
   :synopsis: IPython-based plugin for configuring/controlling the Power PMAC via command-line
.. moduleauthor:: Ken Lauer <klauer@bnl.gov>
"""

from __future__ import print_function
import logging
import os
import sys
import time

import matplotlib.pyplot as plt
import numpy as np

# IPython
import IPython.utils.traitlets as traitlets
from IPython.config.configurable import Configurable
from IPython.core.magic_arguments import (argument, magic_arguments,
                                          parse_argstring)

# Ppmac
import ppmac_util as util
from ppmac_util import PpmacExport
from pp_comm import (PPComm, TimeoutError)
from pp_comm import GPError
import ppmac_gather as gather
import ppmac_completer as completer
import ppmac_tune as tune
import ppmac_const as const

logger = logging.getLogger('PpmacCore')
MODULE_PATH = os.path.dirname(os.path.abspath(__file__))


# Extension Initialization #
def load_ipython_extension(ipython):
    if PpmacCore.instance is not None:
        print('PpmacCore already loaded')
        return None

    logging.basicConfig()

    instance = PpmacCore(shell=ipython, config=ipython.config)
    PpmacCore.instance = instance

    util.export_magic_by_decorator(ipython, globals())
    util.export_class_magic(ipython, instance)
    return instance


def unload_ipython_extension(ipython):
    instance = PpmacCore.instance
    if instance is not None:
        PpmacCore.instance = None
        return True

# end Extension Initialization #


def shell_function_wrapper(exe_):
    """
    (decorator)
    Allows for shell commands to be directly added to IPython's
    user namespace
    """
    def wrapped(usermagics, args):
        cmd = '"%s" %s' % (exe_, args)
        logger.info('Executing: %s' % (cmd, ))

        shell = PpmacCore.instance.shell
        shell.system(cmd)

    return wrapped


class PpmacCore(Configurable):
    instance = None

    ide_host = traitlets.Unicode('10.0.0.6', config=True)
    host = traitlets.Unicode('10.0.0.98', config=True)
    port = traitlets.Int(22, config=True)
    user = traitlets.Unicode('root', config=True)
    password = traitlets.Unicode('deltatau', config=True)
    auto_connect = traitlets.Bool(True, config=True)

    gather_config_file = traitlets.Unicode('/var/ftp/gather/GatherSetting.txt', config=True)
    gather_output_file = traitlets.Unicode('/var/ftp/gather/GatherFile.txt', config=True)

    default_servo_period = traitlets.Float(0.442673749446657994 * 1e-3, config=True)
    use_completer_db = traitlets.Bool(True, config=True)
    completer_db_file = traitlets.Unicode('ppmac.db', config=True)

    def __init__(self, shell, config):
        PpmacCore.instance = self

        util.__pp_plugin__ = self

        super(PpmacCore, self).__init__(shell=shell, config=config)
        logger.info('Initializing PpmacCore plugin')

        # To be flagged as configurable (and thus show up in %config), this
        # instance should be added to the shell's configurables list.
        if hasattr(shell, 'configurables'):
            shell.configurables.append(self)

        for trait in self.trait_names():
            try:
                change_fcn = getattr(self, '_%s_changed' % trait)
            except AttributeError:
                pass
            else:
                change_fcn(trait, None, getattr(self, trait))

        self.comm = None

        if self.use_completer_db:
            self.completer = None
            self.open_completer_db()

    def open_completer_db(self):
        db_file = self.completer_db_file
        c = None
        if self.comm is not None:
            gpascii = self.comm.gpascii_channel()
        else:
            gpascii = None

        if os.path.exists(db_file):
            try:
                c = completer.start_completer_from_db(db_file, gpascii=gpascii)
            except Exception as ex:
                print('Unable to load current db file: %s (%s) %s' %
                      (db_file, ex.__class__.__name__, ex))
                print('Remove it to try loading from the IDE machine\'s MySql')
                return

        if c is None:
            if os.path.exists(db_file):
                os.unlink(db_file)

            windows_ip = self.ide_host
            ppmac_ip = self.host
            c = completer.start_completer_from_mysql(windows_ip, ppmac_ip, db_file=db_file,
                                                     gpascii=gpascii)

        if c is not None:
            self.completer = c
            self.shell.user_ns['ppmac'] = c
            print('Completer loaded into namespace. Try ppmac.[tab]')

    @magic_arguments()
    @argument('-h', '--host', type=str, help='Power PMAC host IP')
    @argument('-P', '--port', type=int, help='Power PMAC SSH port')
    @argument('-u', '--user', type=str, help='Username (root)')
    @argument('-p', '--password', type=str, help='Password (deltatau)')
    def _connect(self, magic_args, arg):
        """
        Connect to the Delta Tau system via SSH
        """
        args = parse_argstring(self._connect, arg)

        if not args:
            return

        self.connect(args.host, args.port, args.user, args.password)

    def connect(self, host=None, port=None, user=None, password=None):
        if host is None:
            host = self.host

        if port is None:
            port = self.port

        if user is None:
            user = self.user

        if password is None:
            password = self.password

        self.comm = PPComm(host=host, port=port,
                           user=user, password=password)

        if self.use_completer_db:
            self.completer = None
            self.open_completer_db()

    def check_comm(self):
        if self.comm is None:
            if self.auto_connect:
                self.connect()
            else:
                logger.error('Not connected')
        return (self.comm is not None)

    @magic_arguments()
    @argument('cmd', nargs='+', type=unicode,
              help='Command to send')
    @argument('-t', '--timeout', type=int, default=0.5,
              help='Time to wait for a response (s)')
    def gpascii(self, magic_args, arg):
        """
        Send a command via gpascii
        (Aliased to g)
        """
        if not self.check_comm():
            return

        args = parse_argstring(self.gpascii, arg)

        if not args:
            return

        gpascii = self.comm.gpascii_channel()
        line = ' '.join(args.cmd)
        gpascii.send_line(line)
        try:
            for line in gpascii.read_timeout(timeout=args.timeout):
                if line:
                    print(line)

        except (KeyboardInterrupt, TimeoutError):
            pass

    g = gpascii

    @magic_arguments()
    @argument('variable', type=unicode, help='Variable to get')
    def get_var(self, magic_args, arg):
        if not self.check_comm():
            return

        args = parse_argstring(self.get_var, arg)

        if not args:
            return

        try:
            print('%s=%s' % (args.variable, self.comm.gpascii.get_variable(args.variable)))
        except GPError as ex:
            print(ex)

    @magic_arguments()
    @argument('variable', type=unicode, help='Variable to set')
    @argument('value', type=unicode, help='Value')
    def set_var(self, magic_args, arg):
        if not self.check_comm():
            return

        args = parse_argstring(self.set_var, arg)

        if not args:
            return

        try:
            set_result = self.comm.gpascii.set_variable(args.variable, args.value)
            print('%s=%s' % (args.variable, set_result))
        except GPError as ex:
            print(ex)

    @magic_arguments()
    @argument('variable', type=unicode,
              help='Variable to get/set')
    @argument('value', type=unicode, nargs='?',
              help='Optionally set a value')
    def v(self, magic_args, arg):
        if not self.check_comm():
            return

        args = parse_argstring(self.v, arg)

        if not args:
            return

        if '=' in args.variable:
            var, value = args.variable.split('=')[:2]
        else:
            var, value = args.variable, args.value

        try:
            if value is None:
                print('%s=%s' % (var, self.comm.gpascii.get_variable(var)))
            else:
                print('%s=%s' % (var, self.comm.gpascii.set_variable(var, value)))
        except GPError as ex:
            print(ex)

    @PpmacExport
    def shell_cmd(self, command):
        """
        Send a shell command (e.g., ls)
        """

        if not command or not self.check_comm():
            return

        for line in self.comm.shell_command(command):
            print(line.rstrip())

    @magic_arguments()
    @argument('first_motor', default=1, nargs='?', type=int,
              help='First motor to show')
    @argument('nmotors', default=10, nargs='?', type=int,
              help='Number of motors to show')
    def motors(self, magic_args, arg):
        """
        Show motor positions
        """

        args = parse_argstring(self.motors, arg)

        if not args or not self.check_comm():
            return

        range_ = range(args.first_motor, args.first_motor + args.nmotors)

        def get_values(var, type_=float):
            var = 'Motor[%%d].%s' % var
            return [self.comm.gpascii.get_variable(var % i, type_=type_)
                    for i in range_]

        act_pos = get_values('ActPos')
        home_pos = get_values('HomePos')
        rel_pos = [act - home for act, home in zip(act_pos, home_pos)]

        for m, pos in zip(range_, rel_pos):
            print('Motor %2d: %.3g' % (m, pos))

    @property
    def servo_period(self):
        if not self.check_comm():
            return self.default_servo_period

        return self.comm.gpascii.get_variable('Sys.ServoPeriod', type_=float) * 1e-3

    @magic_arguments()
    @argument('duration', default=1.0, type=float,
              help='Duration to gather (in seconds)')
    @argument('period', default=1, type=int,
              help='Servo-interrupt data gathering sampling period')
    @argument('addresses', default=1, nargs='+', type=unicode,
              help='Addresses to gather')
    def gather(self, magic_args, arg):
        """
        Gather data
        """
        args = parse_argstring(self.gather, arg)

        if not args or not self.check_comm():
            return

        def fix_addr(addr):
            if self.completer:
                addr = str(self.completer.check(addr))

            if not addr.endswith('.a'):
                addr = '%s.a' % addr

            return addr

        addr = [fix_addr(addr) for addr in args.addresses]
        if 'Sys.ServoCount.a' not in addr:
            addr.insert(0, 'Sys.ServoCount.a')

        gather.gather_and_plot(self.comm, addr,
                               duration=args.duration, period=args.period)

    def get_gather_results(self, settings_file=None, verbose=True):
        if verbose:
            print('Reading gather settings...')
        settings = gather.read_settings_file(self.comm, settings_file)
        if 'gather.addr' not in settings:
            print('settings is', settings)
            raise KeyError('gather.addr: Unable to read addresses from settings file (%s)' % settings_file)

        if verbose:
            #print('Settings are: %s' % settings)
            print('Reading gather data... ', end='')
            sys.stdout.flush()

        addresses = settings['gather.addr']
        data = gather.get_gather_results(self.comm, addresses)
        if verbose:
            print('done')

        return settings, data

    @magic_arguments()
    @argument('save_to', type=unicode, nargs='?',
              help='Filename to save to')
    @argument('settings_file', type=unicode, nargs='?',
              help='Gather settings filename')
    @argument('delimiter', type=unicode, nargs='?', default='\t',
              help='Character(s) to put between columns (tab is default)')
    def gather_save(self, magic_args, arg):
        """
        Save gather data to a file

        If `filename` is not specified, the data will be output to stdout
        If `settings_file` is not specified, the default from ppmac_gather.py
            will be used.
        """
        args = parse_argstring(self.gather_save, arg)

        if not args or not self.check_comm():
            return

        try:
            settings, data = self.get_gather_results(args.settings_file)
        except KeyError as ex:
            logger.error(ex)
            return

        addresses = settings['gather.addr']

        if args.delimiter is not None:
            delim = args.delimiter
        else:
            delim = '\t'

        if args.save_to is not None:
            print('Saving to', args.save_to)
            gather.gather_data_to_file(args.save_to, addresses, data, delim=delim)
        else:
            print(' '.join('%20s' % addr for addr in addresses))
            for line in data:
                print(' '.join('%20s' % item for item in line))

    def custom_tune(self, script, magic_args, range_var=None, range_values=None):
        if not self.check_comm():
            return

        tune_path = os.path.join(MODULE_PATH, 'tune')
        fn = os.path.join(tune_path, script)
        if not os.path.exists(fn):
            print('Script file does not exist: %s' % fn)

        args = ('motor1', 'distance', 'velocity',
                'dwell', 'accel', 'scurve', 'prog', 'coord_sys',
                'gather', 'motor2', 'iterations', 'kill_after',
                )
        kwargs = dict((name, getattr(magic_args, name)) for name in args
                      if hasattr(magic_args, name))

        gpascii = self.comm.gpascii_channel()
        if range_var is not None:
            return tune.tune_range(gpascii, fn, range_var, range_values,
                                   **kwargs)
        else:
            return tune.custom_tune(self.comm, fn, **kwargs)

    @magic_arguments()
    @argument('filename', type=unicode, nargs='?',
              help='Gather settings filename')
    def gather_config(self, magic_args, arg):
        """
        Plot the most recent gather data
        """
        args = parse_argstring(self.gather_config, arg)

        if not args or not self.check_comm():
            return

        if args.filename is None:
            filename = gather.gather_config_file
        else:
            filename = args.filename

        settings = self.comm.read_file(filename)
        for line in settings:
            print(line)

    @magic_arguments()
    @argument('motor', default=1, type=int,
              help='Motor number')
    @argument('settings_file', type=unicode, nargs='?',
              help='Gather settings filename')
    def tune_plot(self, magic_args, arg):
        """
        Plot the most recent gather data for `motor`
        """
        args = parse_argstring(self.tune_plot, arg)

        if not args or not self.check_comm():
            return

        try:
            settings, data = self.get_gather_results(args.settings_file)
        except KeyError as ex:
            logger.error(ex)
            return

        addresses = settings['gather.addr']
        data = np.array(data)

        desired_addr = 'motor[%d].despos.a' % args.motor
        actual_addr = 'motor[%d].actpos.a' % args.motor

        if not addresses or data is None or len(data) == 0:
            print('No data gathered?')
            return

        cols = tune.get_columns(addresses, data,
                                'sys.servocount.a', desired_addr, actual_addr)

        x_axis, desired, actual = cols

        fig, ax1 = plt.subplots()
        ax1.plot(x_axis, desired, color='black', label='Desired')
        ax1.plot(x_axis, actual, color='b', alpha=0.5, label='Actual')
        ax1.set_xlabel('Time (s)')
        ax1.set_ylabel('Position (motor units)')
        for tl in ax1.get_yticklabels():
            tl.set_color('b')

        error = desired - actual
        ax2 = ax1.twinx()
        ax2.plot(x_axis, error, color='r', alpha=0.4, label='Following error')
        ax2.set_ylabel('Error (motor units)')
        for tl in ax2.get_yticklabels():
            tl.set_color('r')

        plt.xlim(min(x_axis), max(x_axis))
        plt.title('Motor %d' % args.motor)
        plt.show()

    @magic_arguments()
    @argument('-a', '--all', action='store_true',
              help='Plot all items')
    @argument('-x', '--x-axis', type=unicode,
              help='Address (or index) to use as x axis')
    @argument('-l', '--left', type=unicode, nargs='*',
              help='Left axis addresses (or indices)')
    @argument('-r', '--right', type=unicode, nargs='*',
              help='right axis addresses (or indices)')
    @argument('settings_file', type=unicode, nargs='?',
              help='Gather settings filename')
    @argument('delimiter', type=unicode, nargs='?', default='\t',
              help='Character(s) to put between columns (tab is default)')
    @argument('-L', '--left-scale', type=float, default=1.0,
              help='Scale data on the left axis by this')
    @argument('-R', '--right-scale', type=float, default=1.0,
              help='Scale data on the right axis by this')
    def gather_plot(self, magic_args, arg):
        """
        Plot the most recent gather data
        """
        args = parse_argstring(self.gather_plot, arg)

        if not args or not self.check_comm():
            return

        try:
            settings, data = self.get_gather_results(args.settings_file)
        except KeyError as ex:
            logger.error(ex)
            return

        addresses = settings['gather.addr']
        print("Available addresses:")
        for address in addresses:
            print('\t%s' % address)

        def fix_index(addr):
            try:
                return int(addr)
            except:
                addr_a = '%s.a' % addr
                if addr_a in addresses:
                    return addresses.index(addr_a)
                else:
                    return addresses.index(addr)

        try:
            x_index = fix_index(args.x_axis)
        except:
            x_index = 0

        if args.all:
            half = len(addresses) / 2
            left_indices = range(half)
            right_indices = range(half, len(addresses))
            if x_index in left_indices:
                left_indices.remove(x_index)
            if x_index in right_indices:
                right_indices.remove(x_index)
        else:
            try:
                left_indices = [fix_index(addr) for addr in args.left]
            except TypeError:
                left_indices = []

            try:
                right_indices = [fix_index(addr) for addr in args.right]
            except TypeError:
                right_indices = []

        data = np.array(data)

        for index in left_indices:
            data[:, index] *= args.left_scale

        for index in right_indices:
            data[:, index] *= args.right_scale

        tune.plot_custom(addresses, data, x_index=x_index,
                         left_indices=left_indices,
                         right_indices=right_indices,
                         left_label=', '.join('%s' % arg for arg in args.left),
                         right_label=', '.join('%s' % arg for arg in args.right))
        plt.show()

    @magic_arguments()
    @argument('motor1', default=1, type=int,
              help='Motor number')
    @argument('distance', default=1.0, type=float,
              help='Move distance per step (motor units)')
    @argument('velocity', default=1.0, type=float,
              help='Velocity (motor units/s)')
    @argument('iterations', default=1, type=int, nargs='?',
              help='Steps')
    @argument('-k', '--kill', dest='kill_after', action='store_true',
              help='Kill the motor after the move')
    @argument('-a', '--accel', default=1.0, type=float,
              help='Set acceleration time (mu/ms^2)')
    @argument('-d', '--dwell', default=1.0, type=float,
              help='Dwell time (ms)')
    @argument('-g', '--gather', type=unicode, nargs='*',
              help='Gather additional addresses during move')
    def pyramid(self, magic_args, arg):
        """
        Pyramid move, gather data and plot

        NOTE: This uses a script located in `tune/ramp.txt` to perform the
              motion.
        """
        args = parse_argstring(self.ramp, arg)

        if not args:
            return

        self.custom_tune('pyramid.txt', args)

    @magic_arguments()
    @argument('motor1', default=1, type=int,
              help='Motor number')
    @argument('distance', default=1.0, type=float,
              help='Move distance (motor units)')
    @argument('velocity', default=1.0, type=float,
              help='Velocity (motor units/s)')
    @argument('iterations', default=1, type=int, nargs='?',
              help='Repetitions')
    @argument('-k', '--kill', dest='kill_after', action='store_true',
              help='Kill the motor after the move')
    @argument('-a', '--accel', default=1.0, type=float,
              help='Set acceleration time (mu/ms^2)')
    @argument('-d', '--dwell', default=1.0, type=float,
              help='Dwell time (ms)')
    @argument('-g', '--gather', type=unicode, nargs='*',
              help='Gather additional addresses during move')
    def ramp(self, magic_args, arg):
        """
        Ramp move, gather data and plot

        NOTE: This uses a script located in `tune/ramp.txt` to perform the
              motion.
        """
        args = parse_argstring(self.ramp, arg)

        if not args:
            return

        self.custom_tune('ramp.txt', args)

    @magic_arguments()
    @argument('script', default='ramp.txt', type=unicode,
              help='Tuning script to use (e.g., ramp.txt)')
    @argument('motor1', default=1, type=int,
              help='Motor number')
    @argument('distance', default=1.0, type=float,
              help='Move distance (motor units)')
    @argument('velocity', default=1.0, type=float,
              help='Velocity (motor units/s)')
    @argument('iterations', default=1, type=int, nargs='?',
              help='Repetitions')
    @argument('-k', '--kill', dest='kill_after', action='store_true',
              help='Kill the motor after the move')
    @argument('-a', '--accel', default=1.0, type=float,
              help='Set acceleration time (mu/ms^2)')
    @argument('-d', '--dwell', default=1.0, type=float,
              help='Dwell time (ms)')
    #@argument('-g', '--gather', type=unicode, nargs='*',
    #          help='Gather additional addresses during move')
    @argument('-v', '--variable', default='Kp', type=unicode,
              help='Parameter to vary')
    @argument('-V', '--values', type=float, nargs='+',
              help='Values to try')
    @argument('-l', '--low', type=float, nargs='?',
              help='Low value')
    @argument('-h', '--high', type=float, nargs='?',
              help='High value')
    @argument('-s', '--step', type=float, nargs='?',
              help='Step')
    def tune_range(self, magic_args, arg):
        """
        for value in values:
            Set parameter = value
            Move, gather data
            Calculate RMS error

        Plots the RMS error with respect to the parameter values.

        values can be specified in --values or as a range:
            % tune_range -v Ki --low 0.0 --high 1.0 --step 0.1
            % tune_range -v Ki --values 0.0 0.1 0.2 ...
        """
        args = parse_argstring(self.tune_range, arg)

        if not args:
            return

        param = args.variable
        if args.values is not None:
            values = args.values
        elif None not in (args.low, args.high, args.step):
            values = np.arange(args.low, args.high, args.step)
        else:
            print('Must set either --values or --low/--high/--step')
            return

        best, rms = self.custom_tune(args.script, args,
                                     range_var=param, range_values=values)

        if len(values) == len(rms):
            plt.plot(values, rms)
            if best is not None:
                plt.vlines(best, min(rms), max(rms))

            plt.ylabel('RMS error')
            plt.xlabel(param)
            plt.show()

    def other_trajectory(move_type):
        @magic_arguments()
        @argument('motor', default=1, type=int,
                  help='Motor number')
        @argument('distance', default=1.0, type=float,
                  help='Move distance (motor units)')
        @argument('velocity', default=1.0, type=float,
                  help='Velocity (motor units/s)')
        @argument('reps', default=1, type=int, nargs='?',
                  help='Repetitions')
        @argument('-k', '--kill', dest='no_kill', action='store_true',
                  help='Don\'t kill the motor after the move')
        @argument('-o', '--one-direction', dest='one_direction', action='store_true',
                  help='Move only in one direction')
        @argument('-a', '--accel', default=1.0, type=float,
                  help='Set acceleration time (mu/ms^2)')
        @argument('-d', '--dwell', default=1.0, type=float,
                  help='Dwell time after the move (ms)')
        def move(self, magic_args, arg):
            """
            Move, gather data and plot

            NOTE: This uses the tuning binaries from the Power PMAC.
            """
            args = parse_argstring(move, arg)

            if not args or not self.check_comm():
                return

            cmd = tune.other_trajectory(move_type, args.motor, args.distance,
                                        velocity=args.velocity, accel=args.accel,
                                        dwell=args.dwell, reps=args.reps,
                                        one_direction=args.one_direction,
                                        kill=not args.no_kill)

            addrs, data = tune.run_tune_program(self.comm, cmd)
            tune.plot_tune_results(addrs, data)
        return move

    dt_ramp = other_trajectory(tune.OT_RAMP)
    dt_trapezoid = other_trajectory(tune.OT_TRAPEZOID)
    dt_scurve = other_trajectory(tune.OT_S_CURVE)

    @magic_arguments()
    @argument('motor', default=1, type=int,
              help='Motor number')
    @argument('text', default='', type=str, nargs='*',
              help='Text to search for (optional)')
    def servo(self, magic_args, arg):
        """
        """
        args = parse_argstring(self.servo, arg)

        if not args or not self.check_comm():
            return

        search_text = ' '.join(args.text).lower()
        for obj, value in tune.get_settings(self.comm.gpascii, args.motor,
                                            completer=self.completer):
            if isinstance(obj, completer.PPCompleterNode):
                try:
                    desc = obj.row['Comments']
                except KeyError:
                    desc = ''

                line = '%15s = %-30s [%s]' % (obj.name, value, desc)
            else:
                line = '%15s = %s' % (obj, value)

            if not search_text:
                print(line)
            else:
                if search_text in line.lower():
                    print(line)

    @magic_arguments()
    @argument('motor_from', default=1, type=int,
              help='Motor number to copy from')
    @argument('motor_to', default=1, type=int,
              help='Motor number to copy to')
    def servo_copy(self, magic_args, arg):
        """
        Copy servo settings from one motor to another
        """
        args = parse_argstring(self.servo_copy, arg)

        if not args or not self.check_comm():
            return

        if args.motor_from == args.motor_to:
            logger.error('Destination motor should be different from source motor')
            return

        tune.copy_settings(self.comm.gpascii, args.motor_from, args.motor_to,
                           completer=self.completer)

    @magic_arguments()
    @argument('variable', type=unicode,
              help='Variable to search')
    @argument('text', type=unicode,
              help='Text to search for')
    def search(self, magic_args, arg):
        """
        Search for `text` in Power PMAC `variable`

        e.g., search motor[1] servo
                searches for 'servo' related entries
        """

        if self.completer is None:
            print('Completer not configured')
            return

        args = parse_argstring(self.search, arg)

        if not args:
            return

        obj = self.completer.check(args.variable)
        items = obj.search(args.text)

        # TODO print in a table
        def fix_row(row):
            return ' | '.join([str(item) for item in row
                              if item not in (u'NULL', None)])
        for key, info in items.items():
            row = fix_row(info.values())
            print('%s: %s' % (key, row))

    @magic_arguments()
    @argument('num', default=1, type=int,
              help='Encoder table number')
    @argument('cutoff', default=100.0, type=float,
              help='Cutoff frequency (Hz)')
    @argument('damping', default=0.7, nargs='?', type=float,
              help='Damping ratio (0.7)')
    def enc_filter(self, magic_args, arg):
        """
        Setup tracking filter on EncTable[]

        Select cutoff frequency fc (Hz) = 1 / (2 pi Tf)
        Typically 100 ~ 200 Hz for resolver, 500 Hz ~ 1 kHz for sine encoder
        Select damping ratio r (typically = 0.7)
        Compute natural frequency wn = 2 pi fc
        Compute sample time Ts = Sys.ServoPeriod / 1000
        Compute Kp term .index2 = 256 - 512 * wn * .n * Ts
        Compute Ki term .index1 = 256 * .n2 * Ts2
        """

        args = parse_argstring(self.enc_filter, arg)

        if not args or not self.check_comm():
            return

        servo_period = self.servo_period
        if args.cutoff <= 0.0:
            i1, i2 = 0, 0
        else:
            i1, i2 = util.tracking_filter(args.cutoff, args.damping,
                                          servo_period=servo_period)

        v1 = 'EncTable[%d].index1' % args.num
        v2 = 'EncTable[%d].index2' % args.num
        for var, value in zip((v1, v2), (i1, i2)):
            self.set_verbose(var, value)

    def set_verbose(self, var, value):
        """
        Set and then get the gpascii variable
        """
        self.comm.gpascii.set_variable(var, value)
        print('%s = %s' % (var, self.comm.gpascii.get_variable(var)))

    @magic_arguments()
    @argument('-d', '--disable', default=False, action='store_true',
              help='Disable WpKey settings (set to 0)')
    def wpkey(self, magic_self, arg):
        args = parse_argstring(self.wpkey, arg)

        if not args or not self.check_comm():
            return

        enabled_str = '$AAAAAAAA'
        if args.disable:
            print('Disabling')
            self.set_verbose('Sys.WpKey', '0')
        else:
            print('Enabling')
            self.set_verbose('Sys.WpKey', enabled_str)

    @magic_arguments()
    @argument('name', type=unicode,
              help='Executable name')
    @argument('source_files', type=unicode, nargs='+',
              help='Source files')
    @argument('-d', '--dest', type=unicode, nargs='?',
              default='/var/ftp/usrflash',
              help='Destination path for files')
    @argument('-r', '--run', type=unicode, nargs='*',
              default='',
              help='Run the built program, with specified arguments')
    def util_build(self, magic_self, arg):
        args = parse_argstring(self.util_build, arg)

        if not args or not self.check_comm():
            return

        return build_utility(self.comm, args.source_files, args.name,
                             dest_path=args.dest, verbose=True,
                             run=args.run)

    @magic_arguments()
    @argument('module', type=unicode,
              help='Kernel module remote filename')
    @argument('name', type=unicode,
              help='Phase function name')
    @argument('motors', type=int, nargs='+',
              help='Motor number(s)')
    @argument('-u', '--unload', action='store_true',
              help='Unload kernel module first (reload)')
    def userphase(self, magic_self, arg):
        args = parse_argstring(self.userphase, arg)

        if not args or not self.check_comm():
            return

        if args.unload:
            self.comm.shell_command('rmmod %s' % args.module, verbose=True)

        for motor in args.motors:
            self.set_variable('Motor[%d].PhaseCtrl' % motor, 0)

        self.comm.shell_command('insmod %s' % args.module, verbose=True)
        self.comm.shell_command('lsmod |grep %s' % args.module, verbose=True)

        for motor in args.motors:
            self.comm.shell_command('/var/ftp/usrflash/userphase -l %d %s' % (motor, args.name),
                                    verbose=True)

        for motor in args.motors:
            self.comm.gpascii.set_variable('Motor[%d].PhaseCtrl' % motor, 1)

    @magic_arguments()
    @argument('coord', type=int,
              help='Coordinate system')
    @argument('program', type=int,
              help='Program number')
    @argument('variables', nargs='*', type=unicode,
              help='Variables to monitor while running')
    def prog_run(self, magic_self, arg):
        args = parse_argstring(self.prog_run, arg)

        if not args or not self.check_comm():
            return

        gpascii = self.comm.gpascii_channel()
        try:
            gpascii.program(args.coord, args.program, start=True)
        except GPError as ex:
            print(ex)
            if 'READY TO RUN' in str(ex):
                print('Are all motors in the coordinate system in closed loop?')
            return

        time.sleep(0.1)

        print('Coord %d Program %d' % (args.coord, args.program))
        active_var = 'Coord[%d].ProgActive' % args.program

        def get_active():
            return gpascii.get_variable(active_var, type_=int)

        last_values = [gpascii.get_variable(var)
                       for var in args.variables]

        for var, value in zip(args.variables, last_values):
            print('%s = %s' % (var, value))

        try:
            while get_active():
                if not args.variables:
                    time.sleep(0.1)
                else:
                    values = [gpascii.get_variable(var)
                              for var in args.variables]
                    for var, old_value, new_value in zip(args.variables,
                                                         last_values, values):
                        if old_value != new_value:
                            print('%s = %s' % (var, new_value))

                    last_values = values

        except KeyboardInterrupt:
            if get_active():
                print("Aborting...")
                gpascii.program(args.coord, args.program, stop=True)

        print('Done (%s = %s)' % (active_var, get_active()))

        error_status = 'Coord[%d].ErrorStatus' % args.coord
        errno = gpascii.get_variable(error_status, type_=int)

        if errno in const.coord_errors:
            print('Error: (%s) %s' % (const.coord_errors[errno]))

    @magic_arguments()
    @argument('script', type=str,
              help='Motion program filename')
    @argument('motors', nargs='*', type=unicode,
              help='In the form X=1')
    @argument('-c', '--coord', type=int, default=0,
              help='Coordinate system')
    @argument('-p', '--program', type=int, default=999,
              help='Program number')
    @argument('-r', '--run', action='store_true',
              help='Run the uploaded program')
    @argument('-g', '--gather', action='store_true',
              help='Use gather')
    def prog_send(self, magic_self, arg):
        """
        Send and optionally run a motion program.

        If gather mode is enabled, the program will be run and the gather
        data will be read.

        Motors are in the form of
                (coordinate system axis)=(motor number)
        If any motors are specified, the coordinate system will be cleared
        first and reassigned.

        """
        args = parse_argstring(self.prog_send, arg)

        if not args or not self.check_comm():
            return

    @magic_arguments()
    @argument('variables', nargs='+', type=unicode,
              help='Variables to monitor')
    def monitor(self, magic_self, arg):
        '''
        Low-speed (compared to gather) monitoring of variables
        '''
        args = parse_argstring(self.monitor, arg)

        if not args or not self.check_comm():
            return

        monitor_variables(args.variables)

    @magic_arguments()
    @argument('base', type=unicode,
              help='Variable to monitor')
    @argument('ignore', nargs='*', type=unicode,
              help='Variable(s) to ignore')
    def monitorc(self, magic_self, arg):
        '''
        Low-speed (compared to gather) monitoring of variables
        using the completer.

        >>> monitorc Motor[1]
            monitors PosSf, Pos, etc.
        '''
        args = parse_argstring(self.monitorc, arg)

        if not args or not self.check_comm():
            return

        if self.completer is None:
            print('Completer not enabled')
            return

        def get_variables(var):
            obj = self.completer.check(var)
            return ['%s.%s' % (var, attr) for attr in dir(obj)]

        variables = get_variables(args.base)
        for ignore in args.ignore:
            if ignore in variables:
                variables.remove(ignore)

        print('Initial values:')
        last_values = get_var_values(self.comm, variables)
        for var, value in zip(variables, last_values):
            print('%s = %s' % (var, value))

        print()
        print('-')

        change_set = set()

        try:
            while True:
                values = get_var_values(self.comm, variables)
                for var, old_value, new_value in zip(variables,
                                                     last_values, values):
                    if old_value != new_value:
                        if new_value.startswith('Error:'):
                            continue

                        print('%s = %s' % (var, new_value))
                        change_set.add(var)

                last_values = values

        except KeyboardInterrupt:
            if change_set:
                print("Variables changed:")
                for var in sorted(change_set):
                    print(var)

    @magic_arguments()
    @argument('motor', default=1, type=int,
              help='Motor number')
    @argument('additional', nargs='*', type=unicode,
              help='Additional fields to check')
    @argument('-i', '--ignore', nargs='*', type=unicode,
              help='Fields to ignore')
    @argument('-a', '--all', action='store_true',
              help='Show all information')
    @argument('-m', '--monitor', action='store_true',
              help='Monitor continuously for changes')
    def mstatus(self, magic_self, arg):
        '''
        Show motor status

        Defaults to showing only possible error/warning values
        (that is, those that differ from ppmac_const.motor_normal)
        '''
        args = parse_argstring(self.mstatus, arg)

        if not args or not self.check_comm():
            return

        motor = 'Motor[%d]' % args.motor
        variables = list(const.motor_status)
        if args.ignore is not None:
            for variable in args.ignore:
                try:
                    variables.remove(variable)
                except:
                    pass

        if args.additional is not None:
            variables.extend(args.additional)

        variables = ['.'.join((motor, var)) for var in variables]
        last_values = {}

        def got_value(var, value):
            var = var.split('.')[-1]
            if args.all or var in args.additional:
                ret = value
            elif var in const.motor_normal:
                last_value = last_values.get(var, None)

                normal_value = const.motor_normal[var]
                if int(value) == normal_value:
                    if last_value is not None:
                        ret = value
                    else:
                        ret = None
                else:
                    # Only include abnormal values
                    ret = value

            last_values[var] = ret
            return ret

        if args.monitor:
            monitor_variables(variables, comm=self.comm, change_callback=got_value)
        else:
            print_var_values(self.comm, variables, cb=got_value)

    @magic_arguments()
    @argument('coord', default=1, type=int,
              help='Coordinate system number')
    @argument('additional', nargs='*', type=unicode,
              help='Additional fields to check')
    @argument('-i', '--ignore', nargs='*', type=unicode,
              help='Fields to ignore')
    @argument('-a', '--all', action='store_true',
              help='Show all information')
    @argument('-m', '--monitor', action='store_true',
              help='Monitor continuously for changes')
    def cstatus(self, magic_self, arg):
        '''
        Show coordinate system status

        Defaults to showing only possible error/warning values
        (that is, those that differ from ppmac_const.coord_normal)
        '''
        args = parse_argstring(self.cstatus, arg)

        if not args or not self.check_comm():
            return

        coord = 'Coord[%d]' % args.coord
        variables = list(const.coord_status)
        if args.ignore is not None:
            for variable in args.ignore:
                try:
                    variables.remove(variable)
                except:
                    pass

        if args.additional is not None:
            variables.extend(args.additional)

        variables = ['.'.join((coord, var)) for var in variables]
        last_values = {}

        def got_value(var, value):
            var = var.split('.')[-1]
            if args.all or var in args.additional:
                ret = value
            elif var in const.coord_normal:
                last_value = last_values.get(var, None)

                normal_value = const.coord_normal[var]
                if int(value) == normal_value:
                    if last_value is not None:
                        ret = value
                    else:
                        ret = None
                else:
                    # Only include abnormal values
                    ret = value

            last_values[var] = ret
            return ret

        if args.monitor:
            monitor_variables(variables, comm=self.comm, change_callback=got_value)
        else:
            print_var_values(self.comm, variables, cb=got_value)


@PpmacExport
def create_util_makefile(source_files, output_name):
    make_path = os.path.join(MODULE_PATH, 'util_makefile')
    makefile = open(make_path, 'rt').read()

    text = makefile % dict(source_files=' '.join(source_files),
                           output_name=output_name)
    return text


@PpmacExport
def build_utility(comm, source_files, output_name,
                  dest_path='/var/ftp/usrflash',
                  verbose=False, cleanup=True,
                  run=None, timeout=0.0,
                  **kwargs):

    makefile_text = create_util_makefile(source_files, output_name)

    comm.send_file(os.path.join(dest_path, 'Makefile'), makefile_text)
    print('Sending Makefile')
    for fn in source_files:
        text = open(fn, 'r').read()
        dest_fn = os.path.join(dest_path, os.path.split(fn)[-1])
        print('Sending', dest_fn)
        comm.send_file(dest_fn, text)

    comm.send_line('cd %s' % dest_path)
    print('Building...')
    lines = comm.shell_command('make', verbose=verbose,
                               **kwargs)

    if cleanup:
        print('Cleaning up...')
        for fn in source_files:
            comm.remove_file(os.path.join(dest_path, fn))
        comm.remove_file(os.path.join(dest_path, 'Makefile'))

    errored = False
    for line in lines:
        if 'error' in line.lower():
            errored = True

    if not errored and run is not None:
        run = ' '.join(run)
        comm.shell_command('%s %s' % (output_name, run),
                           timeout=None, verbose=True)


def get_var_values(comm, vars_, cb=None):
    ret = []
    for var in vars_:
        try:
            value = comm.gpascii.get_variable(var)
        except (GPError, TimeoutError) as ex:
            ret.append('Error: %s' % (ex, ))
        else:
            if cb is not None:
                try:
                    value = cb(var, value)
                except:
                    pass

            ret.append(value)
    return ret


def print_var_values(comm, vars, cb=None, f=sys.stdout):
    values = get_var_values(comm, vars, cb=cb)

    for var, value in zip(vars, values):
        if value is not None:
            print('%s = %s' % (var, value), file=f)

    return values


@PpmacExport
def monitor_variables(variables, f=sys.stdout, comm=None,
                      change_callback=None, show_change_set=False,
                      show_initial=True):
    if comm is None:
        comm = PpmacCore.instance.comm
        if comm is None:
            raise ValueError('PpmacCore comm not connected')

    change_set = set()
    last_values = get_var_values(comm, variables, cb=change_callback)

    if show_initial:
        for var, value in zip(variables, last_values):
            if value is not None:
                print('%s = %s' % (var, value), file=f)

    try:
        while True:
            values = get_var_values(comm, variables, cb=change_callback)
            for var, old_value, new_value in zip(variables,
                                                 last_values, values):
                if new_value is None:
                    continue

                if old_value != new_value:
                    print('%s = %s' % (var, new_value), file=f)
                    change_set.add(var)

            last_values = values

    except KeyboardInterrupt:
        if show_change_set and change_set:
            print("Variables changed:", file=f)
            for var in sorted(change_set):
                print(var, file=f)


@PpmacExport
def print_variables(variables, f=sys.stdout, comm=None,
                    value_callback=None):
    if comm is None:
        comm = PpmacCore.instance.comm
        if comm is None:
            raise ValueError('PpmacCore comm not connected')

    values = get_var_values(comm, variables, cb=value_callback)
    for var, value in zip(variables, values):
        print('%s = %s' % (var, value), file=f)

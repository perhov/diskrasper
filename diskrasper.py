#!/usr/bin/env python3

"""
Overall design
==============

A state machine running in the main thread, and some helper
threads monitoring disk insertion/removal and disk I/O.


State machine
=============

States          Description                     LEDs
-------------   -----------------------------   ------------
IDLE            No disk present                 dark
READY           Disk inserted                   yellow
ERASING         dd running                      yellow blink
IOERROR         dd failed, disk present         red
WIPED           dd succeeded, disk present      green
YANKED          Disk removed while erasing      red
-------------   -----------------------------   ------------

Inputs/Events   Description
-------------   --------------------------------------------
add             A disk is inserted
remove          A disk is removed
button          The button is pressed
dd_ok           dd finishes with exit code 0
dd_fail         dd finishes with error
-------------   --------------------------------------------

State transition table
----------------------

CURRENT STATE → .-----------------------------------------------------------.
        INPUT ↓ |  IDLE   |  READY  | ERASING | IOERROR |  WIPED  | YANKED  |
.---------------|---------|---------|---------|---------|---------|---------|
| add           |  READY  |         |         |         |         |  READY  |
|---------------|---------|---------|---------|---------|---------|---------|
| remove        |         |  IDLE   | YANKED  |  IDLE   |  IDLE   |         |
|---------------|---------|---------|---------|---------|---------|---------|
| button        |    -    | ERASING |    -    |    -    |    -    |  IDLE   |
|---------------|---------|---------|---------|---------|---------|---------|
| dd_ok         |         |         |  WIPED  |         |         |         |
|---------------|---------|---------|---------|---------|---------|---------|
| dd_fail       |         |         | IOERROR |         |         |         |
.---------------.---------.---------.---------.---------.---------.---------.

        [-]: Input is ignored (no transition)
        [ ]: Invalid transition (should never happen, ignore/log as error)


Helper threads
==============

UserInterface:  Thread controlling the LEDs (necessary because of blinking)
DiskMonitor:    Thread listening for udev events (disk insertion/removal)
DiskWiper:      Thread responsible for running/monitoring the 'dd' process

The helper threads sends events to the state machine through a Queue()


"""


import subprocess
import sys
import threading
import time
import traceback
import queue

import pyudev
import RPi.GPIO as GPIO

# GPIO pins for the red/green/blue LEDS, the buzzer, and the button.
GPIOMODE = GPIO.BCM
GPIOPINS = {"R": 10, "G": 9, "B": 11, "buzzer": 8}
GPIOBUTTON = 7

# Name of the device we're watching for (and wiping).
WIPEDEVICE = 'sda'

# Command used to wipe the device.
WIPECMD = ['python', 'dd.py', '/dev/' + WIPEDEVICE]
# WIPECMD = ['bash', '-c', 'sleep 20; exit $[RANDOM%4]']


LOCK = threading.Lock()


def info(msg):
    """Write an informational message to stderr."""
    global LOCK
    LOCK.acquire()
    print(msg, file=sys.stderr)
    LOCK.release()


def debug(msg):
    """Write a debug message to stderr."""
    global LOCK
    LOCK.acquire()
    print("<%s>: %s" % (threading.current_thread().name, msg), file=sys.stderr)
    LOCK.release()


class UserInterface(threading.Thread):
    """
    Helper thread controlling the LEDs.
    To set the LEDs, use the display() method.
    """
    def __init__(self):
        """Initialize thread."""
        threading.Thread.__init__(self)
        self.name = "UIThread"
        self.stopevent = threading.Event()
        self.update = threading.Event()
        self.patterns = {
            # [(led, duration), (led, duration), ...] and duration=None means forever
            "red":    [("R", None)],
            "yellow": [("RG", None)],
            "green":  [("G", None)],
            "blue":   [("B", None)],
            "blank":  [("", None)],
            # "blink":  [("RG", 0.1), ("B", 0.1), ("", 0.1)],  # Alternate yellow/blue
            # "blink":  [("RG", 0.5), ("", 0.5)]  # 1 Hz yellow blinking, 50% duty cycle
            "blink":  [("RG", 0.03), ("", 0.08)] * 4 + [("", 0.11*5)],  # Rhythmic
        }
        self.pattern = self.patterns['blank']
        # Initialize GPIO pins
        for pin in GPIOPINS.values():
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, False)
        # Confirm that we're up and running by beeping and blinking blue once
        GPIO.output(GPIOPINS['buzzer'], True)
        GPIO.output(GPIOPINS['B'], True)
        time.sleep(0.5)
        GPIO.output(GPIOPINS['buzzer'], False)
        GPIO.output(GPIOPINS['B'], False)

    def run(self):
        """Method containing thread's code."""
        while not self.stopevent.is_set():
            for leds, duration in self.pattern:
                # Turn on the LEDs in 'leds' and turn off all others.
                for color in "RGB":
                    GPIO.output(GPIOPINS[color], color in leds)
                # Sleep for the given duration, unless the pattern has changed.
                if self.update.wait(timeout=duration):
                    self.update.clear()
                    break
        debug("Stop event received")

    def stop(self):
        """Method used to terminate the thread."""
        self.stopevent.set()

    def display(self, pat):
        """Displays the pattern named pat (as defined in self.patterns) on the LED."""
        if pat in self.patterns:
            self.pattern = self.patterns[pat]
            self.update.set()


class DiskMonitor(threading.Thread):
    """Helper thread monitoring disk insertion/removal."""
    def __init__(self, statemachine):
        """Initialize thread."""
        threading.Thread.__init__(self)
        self.daemon = True
        self.name = "DiskMonitorThread"
        self.statemachine = statemachine
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem='block', device_type='disk')

    def run(self):
        """Method containing thread's code."""
        try:
            # Check if a disk is already present at startup
            device = pyudev.Device.from_name(self.context, 'block', WIPEDEVICE)
            self._add(device)
        except pyudev.device.DeviceNotFoundByNameError:
            pass
        # Monitor for udev events
        for action, device in self.monitor:
            if device['DEVNAME'] == '/dev/' + WIPEDEVICE:
                if action == 'add':
                    self._add(device)
                elif action == 'remove':
                    self._remove(device)

    def _add(self, device):
        """Method called on device insertion."""
        model = device.get('ID_MODEL', '?')
        serial = device.get('ID_SERIAL_SHORT', '?')
        debug("Disk added: <%s> serial=%s" % (model, serial))
        self.statemachine.event('add')

    def _remove(self, device):  # pylint: disable=unused-argument
        """Method called on device removal."""
        self.statemachine.event('remove')


class DiskWiper(threading.Thread):
    """
    Helper thread for wiping the disk.
    Use the wipe() method to start wiping.
    """
    def __init__(self, statemachine):
        """Initialize thread."""
        threading.Thread.__init__(self)
        self.daemon = True
        self.name = "DiskWiperThread"
        self.proc = None
        self.statemachine = statemachine
        self.wipeevent = threading.Event()

    def abort(self):
        """Stop the wiping process, for example when the disk is yanked."""
        debug("Killing 'dd'!")
        try:
            self.proc.kill()
        except OSError:
            pass

    def wipe(self):
        """Start wiping the disk."""
        self.wipeevent.set()

    def run(self):
        """Method containing thread's code."""
        while True:
            self.wipeevent.wait()
            debug("Starting '%s'" % " ".join(WIPECMD))
            self.wipeevent.clear()
            self.proc = subprocess.Popen(WIPECMD)
            ret = self.proc.wait()
            debug("exit code %d" % ret)
            if ret == 0:
                self.statemachine.event('dd_ok')
            else:
                # We may fail because disk has been yanked and current state is YANKED.
                # If this is the case, don't send an event that dd failed - we know it.
                if self.statemachine.state != 'YANKED':
                    self.statemachine.event('dd_fail')


class StateMachine(object):
    """The state machine keeping track of everything."""
    def __init__(self, transitions, initial):
        """Initialize thread."""
        GPIO.setmode(GPIOMODE)
        self.queue = queue.Queue()
        self.diskmonitor = DiskMonitor(self)
        self.diskwiper = DiskWiper(self)
        self.userinterface = UserInterface()
        self.initial = initial
        self.transitions = transitions
        self.state = None

    def stop(self):
        """Halt the state machine."""
        debug("FSM Stopping")
        self.userinterface.stop()
        self.userinterface.join()
        GPIO.cleanup()
        debug("FSM Stopped")

    def enter(self, newstate):
        """Perform a transition to 'newstate'."""
        info("STATE: %s => %s" % (self.state, newstate))
        if self.state != newstate:
            if hasattr(self, 'leave_%s' % self.state):
                getattr(self, 'leave_%s' % self.state)()
            self.state = newstate
            if hasattr(self, 'enter_%s' % self.state):
                getattr(self, 'enter_%s' % self.state)()

    def run(self):
        """Process incoming events."""
        self.diskmonitor.start()
        self.diskwiper.start()
        self.userinterface.start()
        self.enter(self.initial)
        GPIO.setup(GPIOBUTTON, GPIO.IN)
        GPIO.add_event_detect(GPIOBUTTON, GPIO.RISING, callback=self._button, bouncetime=200)
        while True:
            try:
                event = self.queue.get(timeout=10)
            except queue.Empty:
                continue
            info("EVENT: %s" % event)
            for curstate, trigger, newstate in self.transitions:
                if curstate == self.state and event == trigger:
                    self.enter(newstate)
                    break
            else:
                info("ERROR: Event is invalid in state '%s'" % self.state)

    def _button(self, _channel):
        """Called when the button is pressed."""
        self.event('button')

    def event(self, evt):
        """Used by other threads to queue events to the state machine."""
        self.queue.put(evt)

    def enter_IDLE(self):  # pylint: disable=invalid-name
        """Called upon entering IDLE state."""
        self.userinterface.display("blank")

    def enter_READY(self):  # pylint: disable=invalid-name
        """Called upon entering READY state."""
        self.userinterface.display("yellow")

    def enter_ERASING(self):  # pylint: disable=invalid-name
        """Called upon entering ERASING state."""
        self.diskwiper.wipe()
        self.userinterface.display("blink")

    def enter_IOERROR(self):  # pylint: disable=invalid-name
        """Called upon entering IOERROR state."""
        self.userinterface.display("red")

    def enter_WIPED(self):  # pylint: disable=invalid-name
        """Called upon entering WIPED state."""
        self.userinterface.display("green")

    def enter_YANKED(self):  # pylint: disable=invalid-name
        """Called upon entering YANKED state."""
        self.diskwiper.abort()
        self.userinterface.display("red")


# pylint: disable=bad-whitespace
TRANSITIONS = [
    ('IDLE',    'add',     'READY'),
    ('IDLE',    'button',  'IDLE'),
    ('READY',   'remove',  'IDLE'),
    ('READY',   'button',  'ERASING'),
    ('ERASING', 'remove',  'YANKED'),
    ('ERASING', 'button',  'ERASING'),
    ('ERASING', 'dd_ok',   'WIPED'),
    ('ERASING', 'dd_fail', 'IOERROR'),
    ('IOERROR', 'remove',  'IDLE'),
    ('IOERROR', 'button',  'IOERROR'),
    ('WIPED',   'remove',  'IDLE'),
    ('WIPED',   'button',  'WIPED'),
    ('YANKED',  'add',     'READY'),
    ('YANKED',  'button',  'IDLE')
]
# pylint: enable=bad-whitespace


def main():
    """Main entry point."""
    fsm = StateMachine(transitions=TRANSITIONS, initial='IDLE')
    try:
        fsm.run()
        debug("FSM Really stopped")
    except KeyboardInterrupt:
        debug("Caught KeyboardInterrupt")
        fsm.stop()
    except Exception:  # pylint: disable=broad-except
        debug("Caught Exception")
        traceback.print_exc()
        fsm.stop()


if __name__ == '__main__':
    main()

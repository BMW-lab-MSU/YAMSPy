# messages sent to Cognifly: [vel, arm, time_step_id]
# messages sent by Cognifly:
#   [alt, vel, acc, ubatt, last_time_step_id, cur_time_step_id] when no camera
#   [alt, vel, acc, ubatt, last_time_step_id, cur_time_step_id] when camera
# first time_step_ids are -1, they should be discarded
# Cognifly config: balloon3_debug_agl


__author__ = "Ricardo de Azambuja, Yann Bouteiller"
__copyright__ = "Copyright 2020, MISTLab.ca"
__credits__ = [""]
__license__ = "MIT"
__version__ = "0.0.2"
__maintainer__ = "Ricardo de Azambuja"
__email__ = "ricardo.azambuja@gmail.com"
__status__ = "Development"


import time
import curses
from collections import deque
from itertools import cycle
import picamera
import numpy as np

# from threading import Timer

from yamspy import MSPy

# from picamera_AIY_object_detection import MyAIYInterface

from udp_interface import UDPInterface

SWITCH_MODE_CHR = ord('m')
FORWARD_CHR = ord('8')  # curses.KEY_UP
BACKWARD_CHR = ord('5')  # curses.KEY_DOWN
LEFT_CHR = ord('7')  # curses.KEY_LEFT
RIGHT_CHR = ord('9')  # curses.KEY_RIGHT
LEFTYAW_CHR = ord('4')
RIGHTYAW_CHR = ord('6')
UPWARD_CHR = curses.KEY_PPAGE
DOWNWARD_CHR = curses.KEY_NPAGE
PAUSE_CHR = ord('p')
ARM_CHR = ord('a')
DISARM_CHR = ord('d')
REBOOT_CHR = ord('r')
QUIT_CHR = ord('q')
TAKEOFF_CHR = ord('t')
LAND_CHR = ord('l')

KEY_TIMEOUT = 0.2  # seconds before a keyboard command times out

DEFAULT_ROLL = 1500
DEFAULT_PITCH = 1500
DEFAULT_THROTTLE = 900  # throttle bellow a certain value disarms the FC
DEFAULT_YAW = 1500

MIN_CMD_ROLL = 1000
MIN_CMD_PITCH = 1000
MIN_CMD_THROTTLE = 900  # throttle bellow a certain value disarms the FC
MIN_CMD_YAW = 1400
MAX_CMD_ROLL = 2000
MAX_CMD_PITCH = 2000
MAX_CMD_THROTTLE = 2000
MAX_CMD_YAW = 1600

MIN_VEL_THROTTLE = 1400
MAX_VEL_THROTTLE = 1800

KEY_N_ROLL = 1350
KEY_N_PITCH = 1350
KEY_N_THROTTLE = -10  # added on throttle(-) key press
KEY_N_YAW = 1450
KEY_P_ROLL = 1650
KEY_P_PITCH = 1650
KEY_P_THROTTLE = 10  # added on throttle(+) key press
KEY_P_YAW = 1550

KEY_TAKEOFF = 1800
KEY_LAND = 1000

# DEFAULT_CH5 = 1000  # DISARMED (1000) / ARMED (1800)
# DEFAULT_CH6 = 1500  # ANGLE (1000) / NAV_POSHOLD (1500) <= 1300: NAV ALTHOLD
# DEFAULT_CH7 = 1000  # FAILSAFE (1800)
# DEFAULT_CH8 = 1000  # HEADING HOLD (1800)

DEFAULT_AUX1 = 1000  # DISARMED (1000) / ARMED (1800)
DEFAULT_AUX2 = 1800  # Angle (1000) / NAV_ALTHOLD (1500) / NAV_POSHOLD (1800)

# Max periods for:
CTRL_LOOP_TIME = 1 / 100
SLOW_MSGS_LOOP_TIME = 1 / 5  # these messages take a lot of time slowing down the loop...

NO_OF_CYCLES_AVERAGE_GUI_TIME = 10

OBSERVATIONS_UPDATE_LIST = ['MSP_ALTITUDE', 'MSP_RAW_IMU', 'MSP_DEBUG']  # doesn't include vbatt


def clip(x, min_x, max_x):
    return max(min(x, max_x), min_x)


class CogniflyController():
    def __init__(self,
                 udp_distant_ip=None,
                 udp_local_ip=None,
                 udp_distant_port=None,
                 udp_local_port=None,
                 print_screen=True,
                 obs_loop_time=None,
                 img_in_obs=False):
        """
        Custom controller and udp interface for Cognifly
        If one of udp_distant_ip, udp_distant_port or udp_local_port is None, then the UDP interface is disabled
        Args:
            udp_distant_ip: string (optional): ip of the udp controller
            udp_distant_port: int (optional): port used to send commands to the udp controller
            udp_local_port: int (optional): port used to receive commands from the udp controller
            print_screen: bool (optional):
                if True, messages will be printed repeatedly on the local screen using curses
                if False, only the key presses will be read repeatedly using curses
            obs_loop_time: float (optional):
                if None, an observation is sent by UDP as answer each time a UDP command is received
                else, an observation is sent bu UDP every obs_loop_time seconds
        """
        if udp_distant_ip is None or udp_local_ip is None or udp_distant_port is None or udp_local_port is None:
            self.udp_int = None
        else:
            self.udp_int = UDPInterface()
            self.udp_int.init_receiver(ip=udp_local_ip, port=udp_local_port)
            self.udp_int.init_sender(ip=udp_distant_ip, port=udp_distant_port)
        self.print_screen = print_screen
        self.obs_loop_time = obs_loop_time
        self.slow_ctrl_loop_warn_flag = False
        self.voltage = 0.0
        # self.altitude = 0.0
        self.img_in_obs = img_in_obs
        if self.img_in_obs:
            self.cam = picamera.PiCamera()
            self.cam.resolution = (320, 240)
            self.cam.framerate = 24
            time.sleep(2)
        self.output = np.empty((240, 320, 3), dtype=np.uint8)
        self.aglAlt = 0.0
        self.aglVel = 0.0
        self.rawAcc = 0.0
        self.key_cmd_in_progress = False
        self.udp_armed = False
        self.ludp = 0
        self.last_time_step_id = -1  # last applied action
        self.cur_time_step_id = -1  # new applied action
        self.cur_vel_cmd = 0.0
        self.last_vel_tick = time.time()
        self.last_key_tick = time.time()
        self.last_obs_tick = time.time()

    def run_curses(self):
        result = 1
        try:
            # get the curses screen window
            screen = curses.initscr()
            # turn off input echoing
            curses.noecho()
            # respond to keys immediately (don't wait for enter)
            curses.cbreak()
            # non-blocking
            screen.timeout(0)
            # map arrow keys to special values
            screen.keypad(True)
            if self.print_screen:
                screen.addstr(1, 0, "Press 'q' to quit, 'r' to reboot, 'm' to change mode, 'a' to arm, 'd' to disarm and arrow keys to control", curses.A_BOLD)
            result = self._controller(screen)
        finally:
            # shut down cleanly
            curses.nocbreak()
            screen.keypad(0)
            curses.echo()
            curses.endwin()
            if result == 1:
                print("An error occurred, probably the serial port is not available")

    def _capture_and_send_obs(self, board):
        board.send_RAW_msg(MSPy.MSPCodes['MSP_DEBUG'], data=[])
        dataHandler = board.receive_msg()
        board.process_recv_data(dataHandler)
        self.aglAlt = board.SENSOR_DATA['debug'][2]
        if self.img_in_obs:
            self.cam.capture(self.output, 'rgb')
            self.udp_int.send_msg([self.aglAlt, self.output, self.voltage, self.last_time_step_id, self.cur_time_step_id])
        else:
            self.aglVel = board.SENSOR_DATA['debug'][3]
            board.fast_read_imu()
            self.rawAcc = board.SENSOR_DATA['accelerometer'][2]
            self.udp_int.send_msg([self.aglAlt, self.aglVel, self.rawAcc, self.voltage, self.last_time_step_id, self.cur_time_step_id])

    def _controller(self, screen):

        CMDS = {'roll': DEFAULT_ROLL,
                'pitch': DEFAULT_PITCH,
                'throttle': DEFAULT_THROTTLE,
                'yaw': DEFAULT_YAW,
                'aux1': DEFAULT_AUX1,
                'aux2': DEFAULT_AUX2
                }

        CMDS_ORDER = ['roll', 'pitch', 'throttle', 'yaw', 'aux1', 'aux2']

        # print doesn't work with curses, use addstr instead

        try:
            if self.print_screen:
                screen.addstr(15, 0, "Connecting to the FC...")

            with MSPy(device="/dev/ttyS0", loglevel='WARNING', baudrate=115200) as board:
                if board == 1:  # an error occurred...
                    return 1

                if self.print_screen:
                    screen.addstr(15, 0, "Connecting to the FC... connected!")
                    screen.clrtoeol()
                    screen.move(1, 0)

                average_cycle = deque([0] * NO_OF_CYCLES_AVERAGE_GUI_TIME)

                # It's necessary to send some messages or the RX failsafe will be active
                # and it will not be possible to arm.
                command_list = ['MSP_API_VERSION', 'MSP_FC_VARIANT', 'MSP_FC_VERSION', 'MSP_BUILD_INFO',
                                'MSP_BOARD_INFO', 'MSP_UID', 'MSP_ACC_TRIM', 'MSP_NAME', 'MSP_STATUS', 'MSP_STATUS_EX',
                                'MSP_BATTERY_CONFIG', 'MSP_BATTERY_STATE', 'MSP_BOXNAMES']

                if board.INAV:
                    command_list.append('MSPV2_INAV_ANALOG')
                    command_list.append('MSP_VOLTAGE_METER_CONFIG')

                for msg in command_list:
                    if board.send_RAW_msg(MSPy.MSPCodes[msg], data=[]):
                        dataHandler = board.receive_msg()
                        board.process_recv_data(dataHandler)
                if board.INAV:
                    cellCount = board.BATTERY_STATE['cellCount']
                else:
                    cellCount = 0  # MSPV2_INAV_ANALOG is necessary
                min_voltage = board.BATTERY_CONFIG['vbatmincellvoltage'] * cellCount
                warn_voltage = board.BATTERY_CONFIG['vbatwarningcellvoltage'] * cellCount
                max_voltage = board.BATTERY_CONFIG['vbatmaxcellvoltage'] * cellCount

                if self.print_screen:
                    screen.addstr(15, 0, "apiVersion: {}".format(board.CONFIG['apiVersion']))
                    screen.clrtoeol()
                    screen.addstr(15, 50, "flightControllerIdentifier: {}".format(board.CONFIG['flightControllerIdentifier']))
                    screen.addstr(16, 0, "flightControllerVersion: {}".format(board.CONFIG['flightControllerVersion']))
                    screen.addstr(16, 50, "boardIdentifier: {}".format(board.CONFIG['boardIdentifier']))
                    screen.addstr(17, 0, "boardName: {}".format(board.CONFIG['boardName']))
                    screen.addstr(17, 50, "name: {}".format(board.CONFIG['name']))

                slow_msgs = cycle(['MSP_ANALOG', 'MSP_STATUS_EX', 'MSP_MOTOR', 'MSP_RC'])

                # send the "drone ready" command to the udp controller:
                self.voltage = board.ANALOG['voltage']
                if self.udp_int:
                    self.udp_int.send_msg([self.voltage, 1.0])

                cursor_msg = ""
                last_loop_time = last_slow_msg_time = last_cycleTime = time.time()
                while True:
                    start_time = time.time()

                    #
                    # UDP recv non-blocking  (NO DELAYS) -----------------------
                    # For safety, UDP commands are overridden by key presses
                    #
                    if self.udp_int:
                        udp_cmd = self.udp_int.recv_msg_nonblocking()
                        if udp_cmd is not None:
                            screen.addstr(14, 0, f"DEBUG: received udp_cmd:{udp_cmd}")
                            screen.clrtoeol()
                            ludp = len(udp_cmd)
                            if ludp > 1:
                                # Warn against CTRL too slow compared to UDP
                                self.slow_ctrl_loop_warn_flag = True
                                self.ludp = ludp
                            # keep msg with highest time_step_id (and only if higher than cur_time_step_id)
                            cmd = None
                            for c in udp_cmd:
                                if c[2] >= self.cur_time_step_id:
                                    cmd = c
                                    self.last_time_step_id = self.cur_time_step_id
                                    self.cur_time_step_id = c[2]
                                elif c[2] == -1.0:
                                    cmd = c
                                    break
                            if cmd is not None:
                                if cmd[1] == 1.0 and not self.udp_armed:
                                    self.udp_armed = True
                                    CMDS['aux1'] = 1800
                                    cursor_msg = "UDP_ARMED"
                                elif cmd[1] != 1.0:
                                    self.udp_armed = False
                                    CMDS['aux1'] = 1000
                                    cursor_msg = "UDP_DISARMED"
                                else:
                                    self.cur_vel_cmd = cmd[0]
                            if self.obs_loop_time is None:
                                # answer to the controller:
                                self._capture_and_send_obs(board)
                        if self.obs_loop_time is not None:
                            tick = time.time()
                            if tick - self.last_obs_tick >= self.obs_loop_time:
                                self.last_obs_tick = tick
                                self._capture_and_send_obs(board)
                    #
                    # end of UDP recv non-blocking -----------------------------
                    #

                    char = screen.getch()  # get keypress
                    curses.flushinp()  # flushes buffer
                    #
                    # KEYS (NO DELAYS) -----------------------------------------
                    #
                    if char != -1:
                        self.key_cmd_in_progress = True
                        self.last_key_tick = time.time()

                        if char == QUIT_CHR:
                            break

                        elif char == DISARM_CHR:
                            cursor_msg = 'Disarming...'
                            CMDS['aux1'] = 1000

                        elif char == REBOOT_CHR:
                            if self.print_screen:
                                screen.addstr(3, 0, 'Rebooting...')
                                screen.clrtoeol()
                            board.reboot()
                            time.sleep(0.5)
                            break

                        elif char == ARM_CHR:
                            cursor_msg = 'Arming...'
                            CMDS['aux1'] = 1800

                        elif char == SWITCH_MODE_CHR:
                            if CMDS['aux2'] <= 1300:
                                cursor_msg = 'NAV ALTHOLD Mode...'
                                CMDS['aux2'] = 1500
                            elif 1700 > CMDS['aux2'] > 1300:
                                cursor_msg = 'NAV POSHOLD Mode...'
                                CMDS['aux2'] = 1800
                            elif CMDS['aux2'] >= 1650:
                                cursor_msg = 'Angle Mode...'
                                CMDS['aux2'] = 1000

                        elif char == RIGHT_CHR:
                            # CMDS['roll'] = CMDS['roll'] + 10 if CMDS['roll'] + 10 <= 2000 else CMDS['roll']
                            CMDS['roll'] = KEY_P_ROLL
                            cursor_msg = 'roll(+):{}'.format(CMDS['roll'])

                        elif char == LEFT_CHR:
                            # CMDS['roll'] = CMDS['roll'] - 10 if CMDS['roll'] - 10 >= 1000 else CMDS['roll']
                            CMDS['roll'] = KEY_N_ROLL
                            cursor_msg = 'roll(-):{}'.format(CMDS['roll'])

                        elif char == RIGHTYAW_CHR:
                            CMDS['yaw'] = KEY_P_YAW
                            cursor_msg = 'yaw(+):{}'.format(CMDS['yaw'])

                        elif char == LEFTYAW_CHR:
                            CMDS['yaw'] = KEY_N_YAW
                            cursor_msg = 'yaw(-):{}'.format(CMDS['yaw'])

                        elif char == FORWARD_CHR:
                            # CMDS['pitch'] = CMDS['pitch'] + 10 if CMDS['pitch'] + 10 <= 2000 else CMDS['pitch']
                            CMDS['pitch'] = KEY_P_PITCH
                            cursor_msg = 'pitch(+):{}'.format(CMDS['pitch'])

                        elif char == BACKWARD_CHR:
                            # CMDS['pitch'] = CMDS['pitch'] - 10 if CMDS['pitch'] - 10 >= 1000 else CMDS['pitch']
                            CMDS['pitch'] = KEY_N_PITCH
                            cursor_msg = 'pitch(-):{}'.format(CMDS['pitch'])

                        elif char == UPWARD_CHR:
                            # CMDS['throttle'] = CMDS['throttle'] + KEY_P_THROTTLE if CMDS['throttle'] + KEY_P_THROTTLE <= MAX_CMD_THROTTLE else CMDS['throttle']
                            self.cur_vel_cmd += KEY_P_THROTTLE
                            cursor_msg = 'throttle(+):{}'.format(CMDS['throttle'])

                        elif char == DOWNWARD_CHR:
                            # CMDS['throttle'] = CMDS['throttle'] + KEY_N_THROTTLE if CMDS['throttle'] + KEY_N_THROTTLE >= MIN_CMD_THROTTLE else CMDS['throttle']
                            self.cur_vel_cmd += KEY_N_THROTTLE
                            cursor_msg = 'throttle(-):{}'.format(CMDS['throttle'])

                        elif char == TAKEOFF_CHR:
                            CMDS['throttle'] = KEY_TAKEOFF
                            cursor_msg = 'takeoff throttle:{}'.format(CMDS['throttle'])

                        elif char == LAND_CHR:
                            CMDS['throttle'] = KEY_LAND
                            cursor_msg = 'land throttle:{}'.format(CMDS['throttle'])

                        elif PAUSE_CHR:
                            CMDS['roll'] = DEFAULT_ROLL
                            CMDS['pitch'] = DEFAULT_PITCH
                            CMDS['yaw'] = DEFAULT_YAW

                    elif self.key_cmd_in_progress:  # default behavior
                        if time.time() - self.last_key_tick >= KEY_TIMEOUT:
                            self.key_cmd_in_progress = False
                            CMDS['roll'] = DEFAULT_ROLL
                            CMDS['pitch'] = DEFAULT_PITCH
                            CMDS['yaw'] = DEFAULT_YAW
                    #
                    # End of KEYS ----------------------------------------------
                    #

                    vel_tick = time.time()
                    z_before = CMDS['throttle']
                    delta_throttle = (vel_tick - self.last_vel_tick) * self.cur_vel_cmd
                    self.last_vel_tick = vel_tick
                    if not (delta_throttle <= 0.0 and z_before <= MIN_VEL_THROTTLE) and not (delta_throttle >= 0.0 and z_before >= MAX_VEL_THROTTLE):  # right vel direction
                        z_after = z_before + delta_throttle
                        z_after = clip(z_after, MIN_VEL_THROTTLE, MAX_VEL_THROTTLE)
                        CMDS['throttle'] = z_after

                    #
                    # IMPORTANT MESSAGES (CTRL_LOOP_TIME based) ----------------
                    #
                    if (time.time() - last_loop_time) >= CTRL_LOOP_TIME:
                        last_loop_time = time.time()
                        # Send the RC channel values to the FC
                        if board.send_RAW_RC([CMDS[ki] for ki in CMDS_ORDER]):
                            dataHandler = board.receive_msg()
                            board.process_recv_data(dataHandler)
                    #
                    # End of IMPORTANT MESSAGES --------------------------------
                    #

                    #
                    # SLOW MSG processing (user GUI and voltage) ---------------
                    #
                    if (time.time() - last_slow_msg_time) >= SLOW_MSGS_LOOP_TIME:
                        last_slow_msg_time = time.time()
                        # show flags:
                        if self.slow_ctrl_loop_warn_flag:
                            if self.print_screen:
                                screen.addstr(15, 0, f"WARNING: SLOW CTRL / FAST UDP: {self.ludp} udp msgs received at once", curses.A_BOLD + curses.A_BLINK)
                                screen.clrtoeol()
                            self.slow_ctrl_loop_warn_flag = False

                        next_msg = next(slow_msgs)  # circular list
                        if self.print_screen:  # print screen messages
                            # Read info from the FC
                            if board.send_RAW_msg(MSPy.MSPCodes[next_msg], data=[]):
                                dataHandler = board.receive_msg()
                                board.process_recv_data(dataHandler)

                            if next_msg == 'MSP_ANALOG':
                                self.voltage = board.ANALOG['voltage']
                                voltage_msg = ""
                                if min_voltage < self.voltage <= warn_voltage:
                                    voltage_msg = "LOW BATT WARNING"
                                elif self.voltage <= min_voltage:
                                    voltage_msg = "ULTRA LOW BATT!!!"
                                elif self.voltage >= max_voltage:
                                    voltage_msg = "VOLTAGE TOO HIGH"

                                screen.addstr(8, 0, "Battery Voltage: {:2.2f}V".format(board.ANALOG['voltage']))
                                screen.clrtoeol()
                                screen.addstr(8, 24, voltage_msg, curses.A_BOLD + curses.A_BLINK)
                                screen.clrtoeol()

                            elif next_msg == 'MSP_STATUS_EX':
                                ARMED = board.bit_check(board.CONFIG['mode'], 0)
                                screen.addstr(5, 0, "ARMED: {}".format(ARMED), curses.A_BOLD)
                                screen.clrtoeol()

                                screen.addstr(5, 50, "armingDisableFlags: {}".format(board.process_armingDisableFlags(board.CONFIG['armingDisableFlags'])))
                                screen.clrtoeol()

                                screen.addstr(6, 0, "cpuload: {}".format(board.CONFIG['cpuload']))
                                screen.clrtoeol()
                                screen.addstr(6, 50, "cycleTime: {}".format(board.CONFIG['cycleTime']))
                                screen.clrtoeol()

                                screen.addstr(7, 0, "mode: {}".format(board.CONFIG['mode']))
                                screen.clrtoeol()

                                screen.addstr(7, 50, "Flight Mode: {}".format(board.process_mode(board.CONFIG['mode'])))
                                screen.clrtoeol()

                            elif next_msg == 'MSP_MOTOR':
                                screen.addstr(9, 0, "Motor Values: {}".format(board.MOTOR_DATA))
                                screen.clrtoeol()

                            elif next_msg == 'MSP_RC':
                                screen.addstr(10, 0, "RC Channels Values: {}".format(board.RC['channels']))
                                screen.clrtoeol()

                            screen.addstr(11, 0, "GUI cycleTime: {0:2.2f}ms (average {1:2.2f}Hz)".format((last_cycleTime) * 1000, 1 / (sum(average_cycle) / len(average_cycle))))
                            screen.clrtoeol()

                            screen.addstr(12, 0, f"alt:{self.aglAlt} - vel:{self.aglVel} - acc:{self.rawAcc} - cmd:{self.cur_vel_cmd}")
                            screen.clrtoeol()

                            screen.addstr(13, 0, f"CMDS: throttle: {CMDS['throttle']}")
                            screen.clrtoeol()

                            screen.addstr(3, 0, cursor_msg)
                            screen.clrtoeol()
                        else:  # no message printing:
                            if next_msg == 'MSP_ANALOG':
                                self.voltage = board.ANALOG['voltage']
                    #
                    # end of SLOW MSG ------------------------------------------
                    #

                    # This slows the loop down to CTRL_LOOP_TIME:
                    end_time = time.time()
                    last_cycleTime = end_time - start_time
                    if last_cycleTime < CTRL_LOOP_TIME:
                        time.sleep(CTRL_LOOP_TIME - last_cycleTime)

                    average_cycle.append(last_cycleTime)
                    average_cycle.popleft()

        finally:
            if self.print_screen:
                screen.addstr(5, 0, "Disconnected from the FC!")
                screen.clrtoeol()
            print("Bye!")


def main(args):
    ip_send = args.ipsend
    ip_recv = args.iprecv
    port_send = args.portsend
    port_recv = args.portrecv
    obs_loop_time = args.obslooptime
    img_in_obs = args.camera
    cc = CogniflyController(udp_distant_ip=ip_send, udp_local_ip=ip_recv, udp_distant_port=port_send, udp_local_port=port_recv, obs_loop_time=obs_loop_time, img_in_obs=img_in_obs)
    cc.run_curses()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--ipsend', type=str, default=None, help='IP address of the drone if any.')
    parser.add_argument('--iprecv', type=str, default=None, help='local IP address if any.')
    parser.add_argument('--portsend', type=int, default=8988, help='Port to send udp messages to.')
    parser.add_argument('--portrecv', type=int, default=8989, help='Port to reveive udp messages from.')
    parser.add_argument('--obslooptime', type=float, default=None, help='Duration between sending UDP observations.')
    parser.add_argument('--camera', action='store_true', help='whether to use camera images in observations.')
    args = parser.parse_args()
    main(args)

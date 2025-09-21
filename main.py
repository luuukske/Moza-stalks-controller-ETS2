#!/usr/bin/env python3
"""
MOZA Multi-function Stalks Button Monitor
Continuously scans for button presses on MOZA Multi-function Stalks device.
Enhanced with robust device connection handling and auto-reconnection.
"""

from enum import auto
from importlib import invalidate_caches
import time
import threading
import truck_telemetry
from scscontroller import SCSController
controller = SCSController()
from typing import Optional, Dict, Any

try:
    import hid
except ImportError:
    print("Error: 'hidapi' library not found.")
    print("Install with: pip install hidapi")
    exit(1)

# lights
light_state = 2 # 0=off, 1=parking, 2=low beam
wiper_state = 0 # -1=manual, 0=off/sensor, 1=intermittent, 2=low, 3=high


class MOZAStalksMonitor:
    def __init__(self):
        self.device = None
        self.device_info = None
        self.running = False
        self.connected = False
        self.thread = None
        self.last_state = None
        self.data = None

        self.autodisable = False
        self.autodisable_blinks = 3 # number of blinks to auto-disable when not locked
        self.autodisable_threshold = 1 # blinks

        # Connection management
        self.reconnect_delay = 2.0  # seconds between reconnection attempts
        self.max_read_errors = 5    # max consecutive read errors before reconnecting
        self.read_error_count = 0
        self.switch_cooldown = 0.15  # seconds to ignore rapid turn signal changes

        # running variables
        # turning signals
        self.blink_count = 0 # Number of blinks since last activated
        self.indicator_state = 0  # 0=off, 1=right, -1=left
        self.prev_blinker_state = False
        self.last_turnsignal_time = 0
        self.right_cooldown = False
        self.left_cooldown = False
        # wipers
        self.rain_sensor = False
        
    def find_moza_device(self) -> Optional[Dict[str, Any]]:
        """Find MOZA Multi-function Stalk device - only exact device name match."""
        try:
            devices = hid.enumerate()
        except Exception as e:
            print(f"Error enumerating HID devices: {e}")
            return None
        
        # Look for the exact device name
        target_device_name = "MOZA Multi-function Stalk"
        
        for device in devices:
            product_name = device.get('product_string', '')
            
            # Exact match only
            if product_name == target_device_name:
                print(f"Found target device: {device['product_string']}")
                print(f"VID: {device['vendor_id']:04x}, PID: {device['product_id']:04x}")
                return device
        
        print(f"Target device '{target_device_name}' not found. Available HID devices:")
        for device in devices[:10]:  # Show first 10 devices
            print(f"  {device.get('product_string', 'Unknown')} - "
                  f"VID: {device['vendor_id']:04x}, PID: {device['product_id']:04x}")
        return None
    
    def connect(self) -> bool:
        """Connect to the MOZA device."""
        if self.connected and self.device:
            return True
            
        # Close existing connection if any
        self.disconnect()
        
        device_info = self.find_moza_device()
        if not device_info:
            return False
            
        try:
            self.device = hid.device()
            self.device.open(device_info['vendor_id'], device_info['product_id'])
            self.device.set_nonblocking(True)
            
            self.device_info = device_info
            self.connected = True
            self.read_error_count = 0
            self.last_state = None  # Reset state on reconnection
            
            print(f"Connected to: {device_info['product_string']}")
            return True
            
        except Exception as e:
            print(f"Failed to connect: {e}")
            self.disconnect()
            return False
    
    def disconnect(self):
        """Safely disconnect from the device."""
        self.connected = False
        if self.device:
            try:
                self.device.close()
            except Exception as e:
                print(f"Error closing device: {e}")
            finally:
                self.device = None
    
    def attempt_reconnection(self):
        """Attempt to reconnect to the device."""
        print("Attempting to reconnect...")
        if self.connect():
            print("Reconnection successful!")
            return True
        else:
            print(f"Reconnection failed. Retrying in {self.reconnect_delay} seconds...")
            time.sleep(self.reconnect_delay)
            return False
    
    def monitor_loop(self):
        """Main monitoring loop with connection management."""
        while self.running:
            try:
                # Ensure we're connected
                if not self.connected:
                    if not self.attempt_reconnection():
                        continue
                
                # Read device data
                device_data = self.device.read(64, timeout_ms=50)
                
                # Get game data
                try:
                    self.data = truck_telemetry.get_data()
                except Exception as e:
                    print(f"Error getting telemetry data: {e}")
                    self.data = None
                
                if device_data and self.data:
                    self.process_device_data(device_data)
                    self.proccess_game_data()
                elif device_data:
                    # Process device data even without game data
                    self.process_device_data(device_data)
                
                # Reset error count on successful read
                self.read_error_count = 0
                
            except OSError as e:
                # Device disconnected or communication error
                self.read_error_count += 1
                print(f"Device communication error ({self.read_error_count}/{self.max_read_errors}): {e}")
                
                if self.read_error_count >= self.max_read_errors:
                    print("Max read errors reached. Device may be disconnected.")
                    self.disconnect()
                
                time.sleep(0.1)  # Brief pause before retry
                
            except Exception as e:
                # Other unexpected errors
                print(f"Unexpected error in monitor loop: {e}")
                time.sleep(0.1)
    
    def process_device_data(self, device_data):
        """Process incoming device_data for button changes."""
        if self.last_state is None:
            self.last_state = device_data[:]
            return
            
        # Check for changes in the device_data
        changed = False
        for i, (old_byte, new_byte) in enumerate(zip(self.last_state, device_data)):
            if old_byte != new_byte:
                changed = True
                # Check each bit for button changes
                for bit in range(8):
                    old_bit = (old_byte >> bit) & 1
                    new_bit = (new_byte >> bit) & 1
                    
                    if old_bit != new_bit:
                        button_id = i * 8 + bit
                        if new_bit:
                            print(f"Button {button_id} PRESSED")
                            self.on_button_press(button_id)
                        else:
                            print(f"Button {button_id} RELEASED")
                            self.on_button_release(button_id)
            
        self.last_state = device_data[:]

    def proccess_game_data(self):
        """Process game data for indicator auto-disable and wiper speed. this is ran every loop."""
        if not self.data:
            return

        global light_state
        global wiper_state

        if self.last_turnsignal_time < time.time() - self.switch_cooldown:
            if self.right_cooldown:
                self.indicator_state = 1
                self.autodisable = False
                self.right_cooldown = False
            elif self.left_cooldown:
                self.indicator_state = -1
                self.autodisable = False
                self.left_cooldown = False
            
        # off edge detection
        blinker_state = ((self.data.get("blinkerLeftActive", False) and self.data.get("blinkerLeftOn", False) and self.indicator_state == -1) or \
                        (self.data.get("blinkerRightActive", False) and self.data.get("blinkerRightOn", False) and self.indicator_state == 1) \
                        and not self.data.get("lightsHazards", False))
        if self.prev_blinker_state and not blinker_state and self.indicator_state != 0:
            # just turned off, add blink counter
            self.blink_count += 1
            if self.autodisable and self.blink_count >= self.autodisable_blinks:
                self.indicator_state = 0
                self.autodisable = False
                self.blink_count = 0
                print("Indicators OFF (auto-disabled)")

        self.prev_blinker_state = blinker_state

        # send data to game with error handling - FIXED LOGIC
        try:
            # Get current game indicator states
            left_active = self.data.get("blinkerLeftActive", False)
            right_active = self.data.get("blinkerRightActive", False)
            left_on = self.data.get("blinkerLeftOn", False)
            right_on = self.data.get("blinkerRightOn", False)
            
            # Determine what the indicators should be based on our desired state
            left_should_be_active = (self.indicator_state == -1)
            right_should_be_active = (self.indicator_state == 1)
            
            # Initialize both to False
            lblinker_should_set = False
            rblinker_should_set = False
            
            # Only change the desired blinker based on indicator_state
            if self.indicator_state == -1:  # Want left blinker
                lblinker_should_set = left_should_be_active and not left_active
                # Also handle turning OFF left when needed
                if not left_should_be_active and left_active:
                    lblinker_should_set = True
                    
            elif self.indicator_state == 1:  # Want right blinker
                rblinker_should_set = right_should_be_active and not right_active
                # Also handle turning OFF right when needed
                if not right_should_be_active and right_active:
                    rblinker_should_set = True
                    
            elif self.indicator_state == 0:  # Want both off
                if left_active:
                    lblinker_should_set = True
                if right_active:
                    rblinker_should_set = True
            
            # Send the commands only if needed
            if (lblinker_should_set or rblinker_should_set) and not (left_on or right_on):
                setattr(controller, 'lblinker', lblinker_should_set)
                setattr(controller, 'rblinker', rblinker_should_set)
                time.sleep(0.05)
                setattr(controller, 'lblinker', False)
                setattr(controller, 'rblinker', False)
                print(f"Set indicators - Left: {lblinker_should_set}, Right: {rblinker_should_set}")

            
            # Handle lights
            lights_parking = self.data.get("lightsParking", False)
            lights_beam = self.data.get("lightsBeamLow", False)
            
            if lights_beam:
                current_light_state = 2
            elif lights_parking:
                current_light_state = 1
            else:
                current_light_state = 0

            if current_light_state != light_state:
                setattr(controller, 'light', True)
                time.sleep(0.05)
                setattr(controller, 'light', False)
                time.sleep(0.05)
        
            # Wipers
            if self.rain_sensor and wiper_state == 0:
                # set to sensor mode
                print("Wipers: sensor mode (rain sensor active, wiper_state=0)")
                self.reset_wipers()
                setattr(controller, 'wipersback', True)
            else:
                print("Wipers: not in sensor mode, resetting wipers")
                setattr(controller, 'wipersback', False)

            if wiper_state == 0 and not self.rain_sensor:
                # off
                print("Wipers: off (wiper_state=0, rain sensor inactive)")
                self.reset_wipers()
                setattr(controller, 'tripreset', True)
            elif wiper_state == 1:
                # intermittent
                print("Wipers: intermittent (wiper_state=1)")
                self.reset_wipers()
                setattr(controller, 'wipersback', True)
            elif wiper_state == 2:
                # low
                print("Wipers: low (wiper_state=2)")
                self.reset_wipers()
                setattr(controller, 'wipers0', True)
            elif wiper_state == 3:
                # high
                print("Wipers: high (wiper_state=3)")
                self.reset_wipers()
                setattr(controller, 'wipers1', True)
            elif wiper_state == -1:
                # manual slow wiping
                print("Wipers: manual slow wipe (wiper_state=-1)")
                self.reset_wipers()
                setattr(controller, 'wipers0', True)
                
        except Exception as e:
            print(f"Error sending controller data: {e}")

    def reset_wipers(self):
        """Reset wipers SDK outputs to false."""
        setattr(controller, 'wipersback', False)
        setattr(controller, 'wipers0', False)
        setattr(controller, 'wipers1', False)
        setattr(controller, 'tripreset', False)

    def on_button_press(self, button_id):
        """Called when a button is pressed. Override this method."""
        global light_state
        global wiper_state

        passed_cooldown = (time.time() - self.last_turnsignal_time) > self.switch_cooldown

        # Handle indicators
        if button_id == 7:
            # Right indicator
            if not passed_cooldown:
                print("Turn signal change ignored due to cooldown")
                self.right_cooldown = True
                return
            if self.indicator_state != 1:  # Reset count when changing to right from any other state
                self.blink_count = 0
                self.prev_blinker_state = False  # Reset blinker state tracking
            self.indicator_state = 1
            self.autodisable = False
            print("Right indicator ON")
        elif button_id == 9:
            # Left indicator
            if not passed_cooldown:
                print("Turn signal change ignored due to cooldown")
                self.left_cooldown = True
                return
            if self.indicator_state != -1:  # Reset count when changing to left from any other state
                self.blink_count = 0
                self.prev_blinker_state = False  # Reset blinker state tracking
            self.indicator_state = -1
            self.autodisable = False
            print("Left indicator ON")
        elif button_id == 8:
            self.last_turnsignal_time = time.time()
            self.left_cooldown = False
            self.right_cooldown = False
            # disable indicators
            if self.blink_count < self.autodisable_threshold:
                self.autodisable = True
                print("Indicators kept active")
            else:
                self.autodisable = False
                self.indicator_state = 0
                self.blink_count = 0
                self.prev_blinker_state = False  # Reset blinker state tracking
                print("Indicators OFF")

        # Handle lights        
        elif button_id == 0:
            # off
            light_state = 0
        elif button_id == 1:
            # parking
            light_state = 1
        elif button_id == 2:
            # low beam
            light_state = 2

        elif button_id == 19:
            wiper_state = -1
        elif button_id == 20:
            wiper_state = 0
        elif button_id == 21:
            wiper_state = 1
        elif button_id == 22:
            wiper_state = 2
        elif button_id == 23:
            wiper_state = 3

    def on_button_release(self, button_id):
        """Called when a button is released. Override this method."""
        pass

    def start(self):
        """Start monitoring."""
        print("Connecting to MOZA Multi-function Stalks...")
        
        # Initial connection attempt
        if not self.connect():
            print("Initial connection failed. Will attempt to reconnect automatically.")
        
        self.running = True
        self.thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.thread.start()
        print("Monitoring started. Press buttons to see events.")
        print("Device will automatically reconnect if disconnected.")
        return True
    
    def stop(self):
        """Stop monitoring."""
        print("Stopping monitor...")
        self.running = False
        
        if self.thread:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                print("Warning: Monitor thread did not stop gracefully")
        
        self.disconnect()
        print("Monitoring stopped.")

    def get_status(self) -> Dict[str, Any]:
        """Get current connection status."""
        return {
            'connected': self.connected,
            'device_name': self.device_info.get('product_string', 'Unknown') if self.device_info else None,
            'running': self.running,
            'read_errors': self.read_error_count,
            'indicator_state': self.indicator_state,
            'blink_count': self.blink_count
        }


def main():
    monitor = MOZAStalksMonitor()
    
    while 1:
        try:
            try:
                truck_telemetry.init()

            except:
                time.sleep(5)
                continue
            if monitor.start():
                print("Press Ctrl+C to exit")
                print("Monitor will automatically handle device disconnections\n")
                
                # Status reporting loop
                last_status_time = 0
                while True:
                    current_time = time.time()
                    
                    # Print status every 15 seconds if not connected
                    if current_time - last_status_time > 15:
                        status = monitor.get_status()
                        if not status['connected']:
                            print(f"Status: Device disconnected - scanning for device every {monitor.reconnect_delay}s...")
                        last_status_time = current_time
                    
                    time.sleep(1)
            else:
                print("\nInitial connection failed, but monitoring started.")
                print("The device will be detected automatically when connected.")
                print("\nTroubleshooting:")
                print("1. Make sure the device is connected")
                print("2. On Linux, you might need sudo or udev rules")
                print("3. Check Windows Device Manager for the device")
            
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            monitor.stop()


if __name__ == "__main__":
    main()
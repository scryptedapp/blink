import asyncio
import json

from aiohttp import ClientSession
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import BlinkException

import scrypted_sdk
from scrypted_sdk import (
    ScryptedDeviceBase,
    DeviceProvider,
    Settings,
    Setting,
    ScryptedInterface,
    ScryptedDeviceType,
    Device,
)

from .camera import BlinkCamera

# Import 2FA specific exception if available
try:
    from blinkpy.helpers.util import BlinkTwoFARequiredError
except ImportError:
    # Fallback if the exception doesn't exist in this version
    BlinkTwoFARequiredError = BlinkException


class BlinkProvider(ScryptedDeviceBase, DeviceProvider, Settings):
    blink: Blink
    devices: dict[str, BlinkCamera] = {}
    session: ClientSession = None
    waiting_for_2fa: bool = False

    def __init__(self, nativeId: str = None) -> None:
        super().__init__(nativeId=nativeId)
        asyncio.create_task(self.start_init())

    def print(self, *args, **kwargs) -> None:
        """Overrides the print() from ScryptedDeviceBase to avoid double-printing in the main plugin console."""
        print(*args, **kwargs)

    @property
    def username(self) -> str:
        return self.storage.getItem("username")
    @username.setter
    def username(self, value: str):
        self.storage.setItem("username", value)

    @property
    def password(self) -> str:
        return self.storage.getItem("password")
    @password.setter
    def password(self, value: str):
        self.storage.setItem("password", value)

    @property
    def auth_data(self) -> dict:
        data = self.storage.getItem("auth_data")
        try:
            if data:
                return json.loads(data)
        except json.JSONDecodeError:
            pass
        return None
    @auth_data.setter
    def auth_data(self, value: dict):
        self.storage.setItem("auth_data", json.dumps(value))

    async def getSettings(self) -> list[Setting]:
        return [
            {
                "title": "Blink Username",
                "key": "username",
                "value": self.username,
            },
            {
                "title": "Blink Password",
                "key": "password",
                "value": self.password,
                "type": "password",
            },
            {
                "title": "2FA Code",
                "key": "2fa",
                "value": "",
            }
        ]

    async def putSetting(self, key: str, value: str) -> None:
        if key == "username":
            self.username = value
            # Clear auth data when username changes
            self.auth_data = None
        elif key == "password":
            self.password = value
            # Clear auth data when password changes
            self.auth_data = None
        elif key == "2fa":
            # Handle 2FA code submission
            self.print(f"2FA code submitted. Blink instance: {self.blink is not None}, Waiting for 2FA: {self.waiting_for_2fa}")
            if value:
                if self.blink and self.waiting_for_2fa:
                    await self.finish_init(value)
                elif not self.blink:
                    self.print("Cannot submit 2FA code: No active Blink session. Please save your username and password first.")
                elif not self.waiting_for_2fa:
                    self.print("Cannot submit 2FA code: Not in 2FA authentication state. Please save your username and password to start authentication.")

                await self.onDeviceEvent(ScryptedInterface.Settings.value, None)
                return
            else:
                self.print("Cannot submit 2FA code: No code provided")
                return
        else:
            raise ValueError(f"Unknown setting key: {key}")

        # Only start init if we don't have auth data and we have credentials
        if not self.auth_data and self.username and self.password:
            await self.start_init()

        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)

    async def start_init(self) -> None:
        if not self.username or not self.password:
            self.print("Blink username and password must be set before initializing.")
            return

        try:
            # Create or reuse session
            if not self.session:
                self.session = ClientSession()

            # Initialize Blink instance
            blink = Blink(session=self.session)
            self.blink = blink

            # Setup auth with credentials or saved data
            if self.auth_data:
                # Try to use saved authentication data
                self.print("Using saved authentication data...")
                blink.auth = Auth(self.auth_data, no_prompt=True)
            else:
                # Fresh login with username/password
                self.print("Starting fresh authentication...")
                blink.auth = Auth({"username": self.username, "password": self.password}, no_prompt=True)

            # Attempt to start Blink
            started = await blink.start()
            if not started:
                # Check if 2FA is required - blink.start() returns False when 2FA is needed
                # The library prints "Two-factor authentication required. Waiting for otp."
                self.print("2FA required. Please enter the code sent to your email in the '2FA Code' field and click Save.")
                self.waiting_for_2fa = True
                # Don't cleanup - we need the session for 2FA submission
                return

            # Success! Save auth data and discover cameras
            self.print("Authentication successful!")
            self.auth_data = blink.auth.login_attributes
            self.waiting_for_2fa = False
            await self.discover_cameras()

        except BlinkTwoFARequiredError:
            # 2FA is required (explicit exception)
            self.print("2FA required. Please enter the code sent to your email in the '2FA Code' field and click Save.")
            self.waiting_for_2fa = True
            # Don't raise - we're waiting for user input

        except Exception as e:
            # Check if this is a 2FA related error (for older versions that don't have specific exception)
            error_msg = str(e)
            if "2FA" in error_msg or "412" in error_msg or "key" in error_msg.lower() or "pin" in error_msg.lower() or "otp" in error_msg.lower():
                self.print("2FA required. Please enter the code sent to your email in the '2FA Code' field and click Save.")
                self.waiting_for_2fa = True
                # Don't raise - we're waiting for user input
            else:
                # Other authentication errors
                self.print(f"Authentication error: {e}")
                await self.cleanup()
                # Don't re-raise to avoid breaking the plugin initialization

    async def finish_init(self, mfa_code: str) -> None:
        """Complete authentication with 2FA code."""
        if not mfa_code:
            self.print("No 2FA code provided")
            return

        if not self.blink or not self.waiting_for_2fa:
            self.print("Not in 2FA authentication state")
            return

        try:
            self.print(f"Submitting 2FA code...")

            # Set the 2FA code in auth data
            self.blink.auth.data["2fa_code"] = mfa_code

            # Call start() again - this will complete the authentication with the 2FA code
            started = await self.blink.start()
            if not started:
                self.print("2FA code may be incorrect or expired. Please try again.")
                # Reset waiting_for_2fa so user can try again
                self.waiting_for_2fa = False
                return

            # Success! Save auth data and discover cameras
            self.print("2FA verification successful!")
            self.auth_data = self.blink.auth.login_attributes
            self.waiting_for_2fa = False
            await self.discover_cameras()

        except Exception as e:
            self.print(f"Error completing 2FA verification: {e}")
            self.waiting_for_2fa = False
            # Don't cleanup - let user try again with different code

    async def discover_cameras(self) -> None:
        """Discover and register cameras from Blink system."""
        try:
            devices = []
            for key, camera in self.blink.cameras.items():
                manifest: Device = {
                    "name": camera.name,
                    "nativeId": camera.camera_id,
                    "info": {
                        "manufacturer": "Blink",
                        "model": camera.product_type,
                        "firmware": camera.version,
                        "serialNumber": camera.serial,
                    },
                    "type": ScryptedDeviceType.Camera.value,
                    "interfaces": [
                        ScryptedInterface.Camera.value,
                        ScryptedInterface.VideoCamera.value,
                        #ScryptedInterface.MotionSensor.value,
                    ],
                }
                devices.append(manifest)
                self.devices[camera.camera_id] = key  # Placeholder for BlinkCamera instance

            self.print(f"Discovered {len(devices)} camera(s)")
            await scrypted_sdk.deviceManager.onDevicesChanged({
                "devices": devices
            })

        except Exception as e:
            self.print(f"Error discovering cameras: {e}")
            raise

    async def cleanup(self) -> None:
        """Clean up resources."""
        self.blink = None
        self.auth_data = None
        self.waiting_for_2fa = False
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None

    async def getDevice(self, nativeId: str) -> ScryptedDeviceBase:
        if nativeId not in self.devices:
            raise ValueError(f"Camera with nativeId {nativeId} not found.")

        if isinstance(self.devices[nativeId], BlinkCamera):
            return self.devices[nativeId]

        key = self.devices[nativeId]
        camera = self.blink.cameras[key]

        blink_camera = BlinkCamera(nativeId=nativeId, blink=self.blink, camera=camera)
        self.devices[nativeId] = blink_camera
        return blink_camera
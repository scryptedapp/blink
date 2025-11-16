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


class BlinkProvider(ScryptedDeviceBase, DeviceProvider, Settings):
    blink: Blink
    devices: dict[str, BlinkCamera] = {}

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
        elif key == "password":
            self.password = value
        elif key == "2fa":
            await self.finish_init(value)
        else:
            raise ValueError(f"Unknown setting key: {key}")

        if not self.auth_data:
            await self.start_init()

        await self.onDeviceEvent(ScryptedInterface.Settings.value, None)

    async def start_init(self) -> None:
        try:
            if not self.username or not self.password:
                raise Exception("Blink username and password must be set before initializing.")

            blink = Blink(session=ClientSession())
            blink.auth = Auth({"username": self.username, "password": self.password}, no_prompt=True)

            # Store blink instance for 2FA handling
            self.blink = blink

            # Try to start with saved auth data if available
            if self.auth_data:
                blink.auth.login_attributes = self.auth_data

            try:
                started = await blink.start()
                if not started:
                    raise Exception("Failed to start Blink client. Check your username and password.")

                # Save the auth data after successful login
                self.auth_data = blink.auth.login_attributes

                # If we got here, either 2FA wasn't needed or we had valid saved credentials
                await self.finish_init("")

            except BlinkException as be:
                # Check if this is a 2FA required error
                error_msg = str(be)
                if "2FA" in error_msg or "key" in error_msg.lower() or "pin" in error_msg.lower():
                    self.print("2FA required. Please enter the code sent to your email in the plugin settings.")
                    # Keep blink instance for 2FA completion
                else:
                    # Other authentication errors
                    self.print(f"Authentication error: {be}")
                    self.blink = None
                    self.auth_data = None
                    raise Exception(f"Failed to authenticate with Blink: {be}")

        except BlinkException as e:
            self.print(f"Error initializing Blink: {e}")
            # Don't clear blink/auth_data if it's a 2FA request
            if "2FA" not in str(e) and "key" not in str(e).lower():
                self.blink = None
                self.auth_data = None
            raise
        except Exception as e:
            self.print(f"Error initializing Blink: {e}")
            self.blink = None
            self.auth_data = None
            raise

    async def finish_init(self, mfa_code: str) -> None:
        try:
            if mfa_code:
                # Send the 2FA code
                await self.blink.auth.send_auth_key(self.blink, mfa_code)
                # Complete the setup after verification
                await self.blink.setup_post_verify()
                # Save the auth data after successful 2FA
                self.auth_data = self.blink.auth.login_attributes
                self.print("2FA verification successful!")

            # Discover and register cameras
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

            await scrypted_sdk.deviceManager.onDevicesChanged({
                "devices": devices
            })

        except Exception as e:
            self.print(f"Error completing 2FA verification: {e}")
            raise

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
import asyncio
from datetime import datetime

from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera as BlinkPyCamera

import scrypted_sdk
from scrypted_sdk import (
    ScryptedDeviceBase,
    Camera,
    VideoCamera,
    MediaObject,
    ResponsePictureOptions,
    RequestPictureOptions,
    RequestMediaStreamOptions,
    ResponseMediaStreamOptions,
    FFmpegInput,
)


class BlinkCamera(ScryptedDeviceBase, Camera, VideoCamera):
    blink: Blink
    camera: BlinkPyCamera

    last_image: bytes = None
    last_image_timestamp: datetime = None
    snapshot_task: asyncio.Task = None

    def __init__(self, nativeId: str, blink: Blink, camera: BlinkPyCamera) -> None:
        super().__init__(nativeId=nativeId)
        self.blink = blink
        self.camera = camera

    async def getPictureOptions(self) -> ResponsePictureOptions:
        return []

    async def takePicture(self, options: RequestPictureOptions = None) -> MediaObject:
        # blinkpy only updates camera.thumbnail on blink.refresh(), so after snap_picture()
        # we refresh until the new thumbnail appears. Blink's cloud takes several seconds,
        # so serve the last image while a new one is fetched in the background, and only
        # block when there is no image yet.
        age = (datetime.now() - self.last_image_timestamp).total_seconds() if self.last_image_timestamp else None
        if self.last_image and age < 60:
            return await scrypted_sdk.mediaManager.createMediaObject(self.last_image, mimeType='image/jpeg')
        if not self.snapshot_task or self.snapshot_task.done():
            self.snapshot_task = asyncio.create_task(self.fetch_picture())
        if not self.last_image:
            await asyncio.shield(self.snapshot_task)
        if not self.last_image:
            raise Exception("Blink snapshot unavailable")
        return await scrypted_sdk.mediaManager.createMediaObject(self.last_image, mimeType='image/jpeg')

    async def fetch_picture(self) -> None:
        try:
            old_thumbnail = self.camera.thumbnail
            await self.camera.snap_picture()
            for _ in range(6):
                await asyncio.sleep(3)
                await self.blink.refresh(force=True)
                if self.camera.thumbnail and self.camera.thumbnail != old_thumbnail:
                    break
            response = await self.camera.get_media()
            if not response or response.status != 200:
                self.print(f"Snapshot failed: thumbnail={self.camera.thumbnail} status={response and response.status}")
                return
            self.last_image = await response.read()
            self.last_image_timestamp = datetime.now()
        except Exception as e:
            self.print(f"Snapshot failed: {e!r}")

    async def getVideoStreamOptions(self) -> list[ResponseMediaStreamOptions]:
        return [
            {
                "id": "default",
                "name": "Cloud Video Stream",
                #"audio": {
                #    "codec": "aac",
                #},
                "audio": None,
                "video": {
                    "codec": "h264",
                },
                "source": "cloud",
                "tool": "ffmpeg",
                "userConfigurable": False,
            }
        ]

    async def getVideoStream(self, options: RequestMediaStreamOptions = None) -> MediaObject:
        msos = await self.getVideoStreamOptions()
        msos = msos[0]
        try:
            stream = await self.camera.init_livestream()
            await stream.start()
            asyncio.create_task(stream.feed())
            msos["container"] = "mpegts"
            url = stream.url
        except Exception as e:
            url = await self.camera.get_liveview()
            msos["container"] = "rtsp"

        ffmpeg_input: FFmpegInput = {
            "url": url,
            "inputArguments": [
                "-i", url,
            ],
            "mediaStreamOptions": msos,
        }
        return await scrypted_sdk.mediaManager.createFFmpegMediaObject(ffmpeg_input)
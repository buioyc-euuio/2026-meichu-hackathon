"""Explicit PipeWire source selection; never silently follow another microphone."""

import asyncio
import json
import os
import shutil
import subprocess

from robot_ble import log


def environment():
    if not hasattr(os, "getuid"):
        raise RuntimeError("PipeWire capture requires Linux")
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def graph():
    if shutil.which("pw-dump") is None or shutil.which("pw-record") is None:
        raise RuntimeError("PipeWire capture requires pw-dump and pw-record")
    result = subprocess.run(["pw-dump"], env=environment(), capture_output=True,
                            text=True, timeout=3, check=True)
    objects = json.loads(result.stdout)
    if not isinstance(objects, list):
        raise ValueError("Invalid PipeWire graph response")
    return objects


def sources(objects):
    result = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Source" and props.get("node.name"):
            result.append((obj["id"], props["node.name"], props.get("node.description", props["node.name"])))
    return result


def resolve_source(selector, objects):
    candidates = sources(objects)
    if selector == "bluetooth":
        matches = [source for source in candidates if source[1].startswith("bluez_input.")]
    else:
        matches = [source for source in candidates if source[1] == selector]
    if len(matches) != 1:
        names = ", ".join(source[1] for source in candidates)
        raise ValueError(f"需要唯一的 PipeWire 麥克風，找到 {len(matches)} 個；可用來源：{names}")
    return matches[0][1]


def list_microphones():
    available = sources(graph())
    if not available:
        raise RuntimeError("PipeWire 沒有可用的麥克風；藍牙耳機需開啟通話模式")
    for _, name, description in available:
        print(f"{description}\n  --pipewire-source {name}")


def verify_link(objects, target, pid):
    source_ids = {identifier for identifier, name, _ in sources(objects) if name == target}
    client_ids = {
        str(obj["id"]) for obj in objects
        if obj.get("type") == "PipeWire:Interface:Client"
        and str(obj.get("info", {}).get("props", {}).get("application.process.id")) == str(pid)
    }
    recorder_ids = set()
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Node":
            props = obj.get("info", {}).get("props", {})
            if (str(props.get("application.process.id")) == str(pid)
                    or str(props.get("client.id")) in client_ids):
                recorder_ids.add(obj["id"])
    incoming = []
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Link":
            info = obj.get("info", {})
            if info.get("input-node-id") in recorder_ids and info.get("state") == "active":
                incoming.append(info.get("output-node-id"))
    if not source_ids or not incoming or any(node not in source_ids for node in incoming):
        raise RuntimeError("PipeWire 麥克風連線中斷或來源改變；不改錄其他裝置")


class PipeWireCapture:
    def __init__(self, target, rate, channels):
        self.target, self.rate, self.channels = target, rate, channels
        self.process = None
        self.stderr_task = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "pw-record", "--target", self.target, "--rate", str(self.rate),
            "--channels", str(self.channels), "--format", "s16", "--latency", "100ms",
            "--properties", json.dumps({"node.dont-reconnect": True}), "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment(),
        )
        self.stderr_task = asyncio.create_task(self._stderr())
        first = await self.read()
        verify_link(await asyncio.to_thread(graph), self.target, self.process.pid)
        return first

    async def _stderr(self):
        async for line in self.process.stderr:
            log(f"🎤 PipeWire: {line.decode(errors='replace').strip()}")

    async def read(self):
        return await asyncio.wait_for(
            self.process.stdout.readexactly(512 * self.channels * 2), timeout=2)

    async def watch(self):
        while True:
            await asyncio.sleep(1)
            if self.process.returncode is not None:
                raise RuntimeError(f"PipeWire recorder exited: {self.process.returncode}")
            objects = await asyncio.to_thread(graph)
            verify_link(objects, self.target, self.process.pid)

    async def close(self):
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                await self.process.wait()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                log(f"❌ PipeWire recorder {self.process.pid} 未退出，終止該程序")
                self.process.kill()
                await self.process.wait()
        if self.stderr_task is not None:
            await self.stderr_task

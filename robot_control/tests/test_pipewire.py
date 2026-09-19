from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "camera_control"))
from inputs.pipewire import resolve_source, verify_link


def source(identifier, name):
    return {"id": identifier, "type": "PipeWire:Interface:Node",
            "info": {"props": {"media.class": "Audio/Source", "node.name": name}}}


class PipeWireSelectionTests(unittest.TestCase):
    def test_bluetooth_selection_requires_exactly_one_input(self):
        graph = [source(1, "alsa_input.camera"), source(2, "bluez_input.test")]
        self.assertEqual(resolve_source("bluetooth", graph), "bluez_input.test")
        self.assertEqual(resolve_source("alsa_input.camera", graph), "alsa_input.camera")
        for invalid in ([graph[0]], graph + [source(3, "bluez_input.other")]):
            with self.assertRaises(ValueError):
                resolve_source("bluetooth", invalid)

    def test_missing_explicit_source_does_not_use_default(self):
        with self.assertRaises(ValueError):
            resolve_source("bluez_input.missing", [source(1, "alsa_input.camera")])

    def test_recorder_link_is_resolved_through_its_client_pid(self):
        graph = [
            source(81, "bluez_input.test"),
            {"id": 99, "type": "PipeWire:Interface:Client",
             "info": {"props": {"application.process.id": 1234}}},
            {"id": 100, "type": "PipeWire:Interface:Node",
             "info": {"props": {"client.id": 99}}},
            {"id": 101, "type": "PipeWire:Interface:Link",
             "info": {"input-node-id": 100, "output-node-id": 81, "state": "active"}},
        ]
        verify_link(graph, "bluez_input.test", 1234)
        graph[-1]["info"]["output-node-id"] = 55
        with self.assertRaises(RuntimeError):
            verify_link(graph, "bluez_input.test", 1234)

    def test_lost_link_is_not_treated_as_silence(self):
        with self.assertRaises(RuntimeError):
            verify_link([source(81, "bluez_input.test")], "bluez_input.test", 1234)


if __name__ == "__main__":
    unittest.main()

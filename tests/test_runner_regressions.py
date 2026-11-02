import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_runner as runner
import oci_a1_hunter as hunter


class CliResponseTests(unittest.TestCase):
    def test_empty_successful_lists_are_empty_collections(self):
        for command in (("ons", "topic", "list"), ("ons", "subscription", "list"),
                        ("compute", "instance", "list"), ("bv", "volume", "list")):
            with self.subTest(command=command), patch.object(
                    hunter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, " \n", "")):
                self.assertEqual(hunter.API(hunter.REGION).call(*command), {"data": []})

    def test_empty_get_and_write_responses_still_stop(self):
        for command, write in ((("resource-manager", "stack", "get"), False),
                               (("ons", "topic", "create"), True),
                               (("ons", "topic", "list"), True)):
            with self.subTest(command=command, write=write), patch.object(
                    hunter.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                with self.assertRaises(hunter.Stop):
                    hunter.API(hunter.REGION).call(*command, write=write)

    def test_failed_list_is_not_treated_as_empty(self):
        with patch.object(hunter.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "NotAuthorizedOrNotFound")):
            with self.assertRaisesRegex(hunter.Stop, "NotAuthorizedOrNotFound"):
                hunter.API(hunter.REGION).call("ons", "topic", "list")

    def test_malformed_json_reports_command_without_response_contents(self):
        with patch.object(hunter.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "private-response-content", "")):
            with self.assertRaises(hunter.Stop) as error:
                hunter.API(hunter.REGION).call("ons", "topic", "list")
        self.assertIn("ons topic list", str(error.exception))
        self.assertNotIn("private-response-content", str(error.exception))


class RunnerStateTests(unittest.TestCase):
    def run_setup(self, callback):
        fake_oci = types.SimpleNamespace(
            config=types.SimpleNamespace(from_file=lambda: {"tenancy": "tenancy"},
                                         validate_config=Mock()),
            retry=types.SimpleNamespace(NoneRetryStrategy=lambda: None),
            exceptions=types.SimpleNamespace(ServiceError=type("ServiceError", (Exception,), {})),
            object_storage=types.SimpleNamespace(ObjectStorageClient=Mock()))
        fake_oci.object_storage.ObjectStorageClient.return_value.get_namespace.return_value.data = "ns"
        store = Mock()
        store.load.return_value = None
        saved = []
        store.save.side_effect = lambda state: saved.append(json.loads(json.dumps(state)))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"oci": fake_oci}))
            stack.enter_context(patch.object(sys, "argv", ["github_runner.py", "setup"]))
            stack.enter_context(patch.object(runner, "RemoteState", return_value=store))
            stack.enter_context(patch.object(runner, "check_home"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            terminal = stack.enter_context(patch.object(runner, "output_terminal"))
            stack.enter_context(patch.object(runner.ShortHunter, "setup_email", autospec=True,
                                             side_effect=callback))
            result = runner.main()
        return result, store, saved, terminal

    def test_setup_error_is_saved_before_temporary_directory_is_removed(self):
        paths = []
        def fail_setup(instance):
            paths.append(instance.path)
            raise hunter.Stop("setup failed")
        result, store, saved, terminal = self.run_setup(fail_setup)
        self.assertEqual(result, 2)
        self.assertEqual(saved[-1]["stopped"], "setup failed")
        self.assertEqual(saved[-1]["outbox"]["event"], "stopped")
        self.assertFalse(paths[0].parent.exists())
        terminal.assert_called_once_with(True)
        store.release.assert_called_once()

    def test_fresh_setup_handles_empty_topics_and_subscriptions(self):
        original_setup = runner.ShortHunter.setup_email
        responses = [
            {"data": {"compartment-id": "compartment"}}, None,
            {"data": {"topic-id": "topic"}}, None,
            {"data": {"id": "subscription", "lifecycle-state": "PENDING"}},
        ]
        results = [subprocess.CompletedProcess([], 0, "" if value is None else json.dumps(value), "")
                   for value in responses]
        with patch.object(hunter.subprocess, "run", side_effect=results) as process:
            result, store, saved, terminal = self.run_setup(original_setup)
        self.assertEqual(result, 0)
        self.assertEqual(saved[-1]["subscription"], "subscription")
        self.assertEqual(saved[-1]["topic"], "topic")
        self.assertEqual(process.call_count, 5)
        terminal.assert_not_called()
        store.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()

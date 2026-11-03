import contextlib
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_rate_limit_defers_runner_without_stopping_schedule(self):
        def throttled(instance):
            raise hunter.RateLimited('HTTP 429')
        result, store, saved, terminal = self.run_setup(throttled)
        self.assertEqual(result, 0)
        self.assertEqual(saved, [])
        terminal.assert_called_once_with(False)
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


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(state_dir=self.temp.name, stack_id=hunter.STACK,
                                       region=hunter.REGION, email=hunter.EMAIL)
        self.api = Mock()
        self.subject = hunter.Hunter(self.args, self.api)

    def cli_error(self, status=429, code='TooManyRequests'):
        text = 'Warning: API key label\nTransientServiceError:\n' + json.dumps({
            'status': status, 'code': code, 'message': 'Too many requests for the user'})
        return subprocess.CompletedProcess([], 1, '', text)

    def test_structured_write_429_is_deferred_without_immediate_retry(self):
        with patch.object(hunter.subprocess, 'run', return_value=self.cli_error()) as process:
            with self.assertRaises(hunter.RateLimited):
                hunter.API(hunter.REGION).call('resource-manager', 'job', 'create-apply-job', write=True)
        self.assertEqual(process.call_count, 1)

    def test_read_429_uses_bounded_backoff_then_defers(self):
        with patch.object(hunter.subprocess, 'run', return_value=self.cli_error()) as process, \
                patch.object(hunter.time, 'sleep') as sleep:
            with self.assertRaises(hunter.RateLimited):
                hunter.API(hunter.REGION).call('resource-manager', 'job', 'list')
        self.assertEqual(process.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_other_write_errors_are_not_classified_as_throttling(self):
        for response in (self.cli_error(500, 'InternalServerError'),
                         subprocess.CompletedProcess([], 1, '', 'request-id 429; connection lost')):
            with self.subTest(response=response.stderr), patch.object(hunter.subprocess, 'run', return_value=response):
                with self.assertRaises(hunter.Stop) as error:
                    hunter.API(hunter.REGION).call('resource-manager', 'job', 'create-apply-job', write=True)
                self.assertNotIsInstance(error.exception, hunter.RateLimited)

    def test_throttled_apply_preserves_plan_and_marks_intent(self):
        self.subject.state['pending'] = 'checked-plan'
        self.api.call.side_effect = hunter.RateLimited('HTTP 429')
        with self.assertRaises(hunter.RateLimited):
            self.subject.submit('APPLY', 'checked-plan')
        saved = json.loads(self.subject.path.read_text())
        self.assertTrue(saved['intent']['throttled'])
        self.assertEqual(saved['pending'], 'checked-plan')
        self.assertNotIn('stopped', saved)
        self.api.call.assert_called_once()

    def test_ambiguous_apply_keeps_unflagged_intent(self):
        self.api.call.side_effect = hunter.Stop('connection timed out')
        with self.assertRaises(hunter.Stop):
            self.subject.submit('APPLY', 'checked-plan')
        self.assertNotIn('throttled', self.subject.state['intent'])
        with self.assertRaises(hunter.Stop):
            self.subject.reconcile([])

    def test_absent_throttled_job_allows_later_retry_from_same_plan(self):
        self.subject.state.update(pending='checked-plan', intent_checks=1,
                                  intent={'name': 'request', 'operation': 'APPLY', 'throttled': True})
        self.subject.reconcile([])
        self.assertEqual(self.subject.state['pending'], 'checked-plan')
        self.assertNotIn('intent', self.subject.state)
        self.assertNotIn('intent_checks', self.subject.state)
        self.api.call.assert_not_called()

    def test_accepted_throttled_job_is_adopted_without_duplicate(self):
        self.subject.state['intent'] = {'name': 'request', 'operation': 'APPLY', 'throttled': True}
        self.subject.reconcile([{'id': 'accepted', 'display-name': 'request', 'operation': 'APPLY',
                                 'lifecycle-state': 'IN_PROGRESS'}])
        self.assertEqual(self.subject.state['pending'], 'accepted')
        self.assertNotIn('intent', self.subject.state)
        self.api.call.assert_not_called()

    def test_duplicate_matching_jobs_still_stop(self):
        self.subject.state['intent'] = {'name': 'request', 'operation': 'APPLY', 'throttled': True}
        with self.assertRaises(hunter.Stop):
            self.subject.reconcile([{'id': value, 'display-name': 'request'} for value in ('one', 'two')])

    def test_next_tick_reuses_succeeded_plan_after_throttling(self):
        store = Mock()
        store.load.return_value = {'stack': hunter.STACK, 'region': hunter.REGION, 'subscription': 'sub',
            'pending': 'checked-plan', 'intent': {'name': 'request', 'operation': 'APPLY', 'throttled': True}}
        def read(*args, **kwargs):
            if args[:3] == ('ons', 'subscription', 'get'):
                return {'data': {'lifecycle-state': 'ACTIVE'}}
            if args[:3] == ('resource-manager', 'job', 'list'):
                return {'data': [{'id': 'checked-plan', 'operation': 'PLAN', 'lifecycle-state': 'SUCCEEDED'}]}
            if args[:3] == ('resource-manager', 'job', 'get'):
                return {'data': {'id': 'checked-plan', 'operation': 'PLAN', 'lifecycle-state': 'SUCCEEDED'}}
            raise AssertionError(args)
        self.api.call.side_effect = read
        subject = runner.ShortHunter(self.args, self.api, store)
        with patch.object(subject, 'require_email'), patch.object(subject, 'fingerprint'), \
                patch.object(subject, 'finish_plan', return_value=False) as resume:
            self.assertFalse(subject.tick('tenancy'))
        resume.assert_called_once_with('tenancy')
        self.assertNotIn('intent', subject.state)


if __name__ == "__main__":
    unittest.main()

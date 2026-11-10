"""Program runs keep partial output on timeout and return complete machine output."""
import json
import subprocess
import unittest
from unittest.mock import patch

from orchestrator.sandbox import ProgramResult, run_program


class ProgramOutputTests(unittest.TestCase):
    def test_timeout_keeps_partial_bytes_instead_of_discarding_them(self):
        exc=subprocess.TimeoutExpired('docker',25,output='起点\n'.encode()+b'x'*3000,stderr=b'partial error')
        with patch('orchestrator.sandbox.find_docker_exe',return_value='docker'),patch('orchestrator.sandbox.subprocess.run',side_effect=exc):
            result=run_program('owned-container','sleep 30',10)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode,124)
        self.assertTrue(result.stdout.startswith('起点\n'))
        self.assertEqual(result.stderr,'partial error')

    def test_machine_consumer_gets_parseable_full_json_without_preview_markers(self):
        raw=json.dumps({'room':'s1','objects':list(range(1500))})
        with patch('orchestrator.sandbox.find_docker_exe',return_value='docker'),patch('orchestrator.sandbox.subprocess.run',return_value=subprocess.CompletedProcess([],0,raw,'')) as call:
            result=run_program('owned-container','probe',10)
        self.assertEqual(json.loads(result.stdout)['objects'][-1],1499)
        self.assertEqual(call.call_count,1)
        self.assertTrue(result.ok)
        self.assertFalse(ProgramResult(1,'','',0.1,False).ok)

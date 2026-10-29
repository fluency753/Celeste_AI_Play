"""Keep machine output complete; expose bounded, recoverable tool previews."""
import json
import subprocess
import unittest
from unittest.mock import patch

from orchestrator.sandbox import PREVIEW_CHARS, ProgramResult, run_program


class ProgramOutputTests(unittest.TestCase):
    def run_fake(self, stdout, stderr='', code=0, publish_failure=False):
        saved=[]
        def external(argv, **kwargs):
            if '/usr/bin/timeout' in argv:
                return subprocess.CompletedProcess(argv,code,stdout,stderr)
            self.assertIn('-I',argv)  # workspace modules cannot shadow trusted imports
            saved.append(kwargs['stdin'].read().decode('utf-8'))
            if publish_failure:return subprocess.CompletedProcess(argv,1,'','read-only workspace')
            return subprocess.CompletedProcess(argv,0,json.dumps({'path':f'/workspace/program-output-{len(saved):024x}/output.txt'}),'')
        with patch('orchestrator.sandbox.find_docker_exe',return_value='docker'),patch('orchestrator.sandbox.subprocess.run',side_effect=external):
            result=run_program('owned-container','python geometry.py',10,save_output=True)
        return result,saved

    def test_geometry_header_and_spinner_tail_survive_with_complete_artifact(self):
        raw='frame=123 bounds=[0,0,800,400] exits=right\n'+''.join(f'SPINNER {i}\n' for i in range(1000))+'LAST SPINNER\n'
        result,saved=self.run_fake(raw)
        data=result.tool_payload()
        self.assertTrue(data['stdout'].startswith('frame=123 bounds='))
        self.assertTrue(data['stdout'].endswith('LAST SPINNER\n'))
        self.assertIn('省略中间',data['stdout'])
        self.assertTrue(data['stdout_truncated'])
        self.assertEqual(data['stdout_chars'],len(raw))
        self.assertEqual(result.stdout,raw)
        self.assertEqual(saved,[raw])
        self.assertRegex(data['stdout_path'],r'^/workspace/program-output-[0-9a-f]{24}/output.txt$')

    def test_unicode_streams_independent_and_no_silent_truncation_at_boundary(self):
        result,saved=self.run_fake('中'*PREVIEW_CHARS,'错误'*2000)
        data=result.tool_payload()
        self.assertFalse(data['stdout_truncated'])
        self.assertEqual(data['stdout'],'中'*PREVIEW_CHARS)
        self.assertIsNone(data['stdout_path'])
        self.assertTrue(data['stderr_truncated'])
        self.assertEqual(data['stderr_chars'],4000)
        self.assertEqual(saved,['错误'*2000])
        self.assertNotIn('省略中间',data['stdout'])

    def test_each_full_stream_has_a_distinct_path_and_is_not_overwritten(self):
        result,saved=self.run_fake('O'*3000,'E'*4000)
        self.assertEqual(saved,['O'*3000,'E'*4000])
        self.assertNotEqual(result.output_files['stdout'],result.output_files['stderr'])

    def test_publish_failure_preserves_exit_code_and_preview_without_fake_path(self):
        result,_=self.run_fake('HEAD'+'x'*3000+'TAIL','err'*1000,code=7,publish_failure=True)
        data=result.tool_payload()
        self.assertEqual(data['returncode'],7)
        self.assertTrue(data['stdout'].startswith('HEAD'))
        self.assertTrue(data['stdout'].endswith('TAIL'))
        self.assertIsNone(data['stdout_path'])
        self.assertEqual(set(data['output_save_errors']),{'stdout','stderr'})

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
        self.assertEqual(result.output_files,{})

    def test_invalid_publisher_path_does_not_publish_success(self):
        for invalid in ({}, {'path':'/workspace/program-output-x/../../foreign/output.txt'}):
            with self.subTest(invalid=invalid):
                replies=[subprocess.CompletedProcess([],0,'HEAD'+'x'*3000+ 'TAIL',''),
                         subprocess.CompletedProcess([],0,json.dumps(invalid),'')]
                with patch('orchestrator.sandbox.find_docker_exe',return_value='docker'),patch('orchestrator.sandbox.subprocess.run',side_effect=replies):
                    data=run_program('owned-container','probe',10,save_output=True).tool_payload()
                self.assertIsNone(data['stdout_path'])
                self.assertIn('stdout',data['output_save_errors'])

    def test_small_or_empty_output_is_complete_without_artifact(self):
        result,saved=self.run_fake('small')
        self.assertEqual(saved,[])
        self.assertFalse(result.tool_payload()['stdout_truncated'])
        self.assertFalse(ProgramResult(1,'','',0.1,False).ok)

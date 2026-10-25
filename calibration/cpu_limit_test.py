"""智能体容器的 CPU 核数限制（可调参数 agent_cpus → docker update --cpuset-cpus）。"""
import subprocess
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.container import apply_cpu_limit


def done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class CpuLimitTests(unittest.TestCase):
    def run_with(self, cpus, ncpu="8"):
        cfg = RunConfig(agent="a", save="s", model="opus")
        cfg.mode = "explore"
        cfg.agent_cpus = cpus
        calls = []

        def docker(*args, check=True, timeout=120):
            calls.append(args)
            return done(ncpu if args[0] == "info" else "")
        with patch("orchestrator.container.docker", side_effect=docker):
            apply_cpu_limit(cfg, "celeste-agent-a", log=lambda m: None)
        return cfg, calls[-1]

    def test_limits_and_unlimited(self):
        cfg, call = self.run_with(4)
        self.assertEqual(call, ("update", "--cpuset-cpus", "0-3", "celeste-agent-a"))
        self.assertEqual(cfg.agent_cpus_effective, 4)
        cfg, call = self.run_with(None)                 # 不限：Docker 的全部核
        self.assertEqual(call[2], "0-7")
        self.assertEqual(cfg.agent_cpus_effective, 8)
        cfg, call = self.run_with(12)                   # 多于 Docker 能用的：按 Docker 的核数
        self.assertEqual((call[2], cfg.agent_cpus_effective), ("0-7", 8))
        cfg, call = self.run_with(1)
        self.assertEqual(call[2], "0")


if __name__ == "__main__":
    unittest.main()

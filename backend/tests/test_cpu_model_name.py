"""CPU model name on ARM and x86. Fork: arya-arav/ServerKit.

ARM kernels have no "model name" in /proc/cpuinfo and platform.processor()
is empty on Linux, so ARM hosts (e.g. Ampere / Neoverse cloud VMs) showed
"Unknown". lscpu decodes the ARM part codes.
"""
import builtins
import io

import pytest

from app.services import system_service
from app.services.system_service import SystemService

ARM_CPUINFO = 'processor\t: 0\nBogoMIPS\t: 50.00\nCPU implementer\t: 0x41\nCPU part\t: 0xd0c\n'
ARM_LSCPU = 'Architecture:  aarch64\nVendor ID:     ARM\nModel name:    Neoverse-N1\nCPU(s):        4\n'
X86_CPUINFO = 'processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: AMD EPYC 7763 64-Core Processor\n'


@pytest.fixture
def fake_host(monkeypatch):
    monkeypatch.setattr(SystemService, '_cpu_model_cache', None)
    monkeypatch.setattr(system_service.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(system_service.platform, 'processor', lambda: '')

    def use(cpuinfo, lscpu=None):
        real_open = builtins.open
        monkeypatch.setattr(builtins, 'open', lambda path, *a, **k: io.StringIO(cpuinfo)
                            if path == '/proc/cpuinfo' else real_open(path, *a, **k))
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if lscpu is None:
                return {'success': False, 'output': '', 'returncode': None}
            return {'success': True, 'output': lscpu, 'returncode': 0}
        monkeypatch.setattr(system_service, 'run_checked', fake_run)
        return calls
    return use


def test_arm_name_comes_from_lscpu(fake_host):
    fake_host(ARM_CPUINFO, ARM_LSCPU)
    assert SystemService._cpu_model() == 'ARM Neoverse-N1'


def test_x86_name_comes_from_cpuinfo_without_running_lscpu(fake_host):
    calls = fake_host(X86_CPUINFO, ARM_LSCPU)
    assert SystemService._cpu_model() == 'AMD EPYC 7763 64-Core Processor'
    assert calls == []


def test_vendor_not_repeated_when_already_in_the_name(fake_host):
    fake_host(ARM_CPUINFO, 'Vendor ID: Intel\nModel name: Intel(R) Xeon(R) Gold 6338\n')
    assert SystemService._cpu_model() == 'Intel(R) Xeon(R) Gold 6338'


def test_unknown_only_when_nothing_answers(fake_host):
    fake_host(ARM_CPUINFO, None)
    assert SystemService._cpu_model() == 'Unknown'

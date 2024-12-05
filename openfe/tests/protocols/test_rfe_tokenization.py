# This code is part of OpenFE and is licensed under the MIT license.
# For details, see https://github.com/OpenFreeEnergy/openfe

from openfe.protocols import openmm_rfe
from gufe.tests.test_tokenization import GufeTokenizableTestsMixin
import pytest

"""
todo:
- RelativeHybridTopologyProtocolResult
- RelativeHybridTopologyProtocol
- RelativeHybridTopologyProtocolUnit
"""

@pytest.fixture
def protocol():
    return openmm_rfe.RelativeHybridTopologyProtocol(openmm_rfe.RelativeHybridTopologyProtocol.default_settings())


@pytest.fixture
def protocol_unit(protocol, benzene_system, toluene_system, benzene_to_toluene_mapping):
    pus = protocol.create(
        stateA=benzene_system, stateB=toluene_system,
        mapping=[benzene_to_toluene_mapping],
    )
    return list(pus.protocol_units)[0]


@pytest.mark.skip
class TestRelativeHybridTopologyProtocolResult(GufeTokenizableTestsMixin):
    cls = openmm_rfe.RelativeHybridTopologyProtocolResult
    repr = ""
    key = ""

    @pytest.fixture()
    def instance(self):
        pass


class TestRelativeHybridTopologyProtocol(GufeTokenizableTestsMixin):
    cls = openmm_rfe.RelativeHybridTopologyProtocol
    key = None
    repr = "<RelativeHybridTopologyProtocol-"

    @pytest.fixture()
    def instance(self, protocol):
        return protocol

    def test_repr(self, instance):
        """
        Overwrites the base `test_repr` call.
        """
        assert isinstance(repr(instance), str)
        assert self.repr in repr(instance)


class TestRelativeHybridTopologyProtocolUnit(GufeTokenizableTestsMixin):
    cls = openmm_rfe.RelativeHybridTopologyProtocolUnit
    repr = "RelativeHybridTopologyProtocolUnit(benzene to toluene repeat"
    key = None

    @pytest.fixture()
    def instance(self, protocol_unit):
        return protocol_unit

    def test_key_stable(self):
        pytest.skip()

    def test_repr(self, instance):
        """
        Overwrites the base `test_repr` call.
        """
        assert isinstance(repr(instance), str)
        assert self.repr in repr(instance)

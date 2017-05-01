from pacman.model.decorators.overrides import overrides
from pacman.model.graphs.machine import MachineVertex
from pacman.model.resources import CPUCyclesPerTickResource, DTCMResource
from pacman.model.resources import ResourceContainer, SDRAMResource

from spinn_front_end_common.utilities import constants, helpful_functions
from spinn_front_end_common.interface.simulation import simulation_utilities
from spinn_front_end_common.abstract_models.impl.machine_data_specable_vertex \
    import MachineDataSpecableVertex
from spinn_front_end_common.abstract_models.abstract_has_associated_binary \
    import AbstractHasAssociatedBinary
from spinn_front_end_common.interface.buffer_management.buffer_models\
    .abstract_receive_buffers_to_host import AbstractReceiveBuffersToHost
from spinn_front_end_common.interface.buffer_management\
    import recording_utilities
from spinn_front_end_common.utilities.utility_objs.executable_start_type \
    import ExecutableStartType

from spinn_front_end_common.abstract_models.abstract_provides_n_keys_for_partition import AbstractProvidesNKeysForPartition

from spinnaker_graph_front_end.utilities.conf import config

from spinn_front_end_common.utilities import constants

from enum import Enum
import logging

logger = logging.getLogger(__name__)


class pfAgg_Vertex(
        MachineVertex, MachineDataSpecableVertex, AbstractHasAssociatedBinary,
        AbstractReceiveBuffersToHost, AbstractProvidesNKeysForPartition):

    DATA_REGIONS = Enum(
        value="DATA_REGIONS",
        names=[('SYSTEM', 0),
               ('TRANSMISSION_DATA', 1),
               ('RECEPTION_BASE_KEYS', 2),
               ('RECORDED_DATA', 3)])

    CORE_APP_IDENTIFIER = 0xBEEF

    def __init__(self, label, constraints=None):
        MachineVertex.__init__(self, label=label, constraints=constraints)
        
        AbstractProvidesNKeysForPartition.__init__(self)

        self._buffer_size_before_receive = None
        if config.getboolean("Buffers", "enable_buffered_recording"):
            self._buffer_size_before_receive = config.getint(
                "Buffers", "buffer_size_before_receive")
        self._time_between_requests = config.getint(
            "Buffers", "time_between_requests")
        self._receive_buffer_host = config.get(
            "Buffers", "receive_buffer_host")
        self._receive_buffer_port = helpful_functions.read_config_int(
            config, "Buffers", "receive_buffer_port")
        self._n_machine_time_steps = None
        self.TRANSMISSION_DATA_SIZE = 16

        self.placement = None

    @property
    @overrides(MachineVertex.resources_required)
    @requires_injection(["TotalMachineTimeSteps"])
    def resources_required(self):
        resources = ResourceContainer(
            cpu_cycles=CPUCyclesPerTickResource(45),
            dtcm=DTCMResource(100), sdram=SDRAMResource((self._n_machine_time_steps*12) + constants.SYSTEM_BYTES_REQUIREMENT +  self.TRANSMISSION_DATA_SIZE))

        resources.extend(recording_utilities.get_recording_resources(
            [self._n_machine_time_steps*12],
            self._receive_buffer_host, self._receive_buffer_port))

        return resources

    @overrides(AbstractHasAssociatedBinary.get_binary_file_name)
    def get_binary_file_name(self):
        return "pfAgg.aplx"

    @overrides(AbstractHasAssociatedBinary.get_binary_start_type)
    def get_binary_start_type(self):
        return ExecutableStartType.USES_SIMULATION_INTERFACE
        
    @overrides(AbstractProvidesNKeysForPartition.get_n_keys_for_partition)
    def get_n_keys_for_partition(self, partition, graph_mapper):
        if(partition = "Resample Data"):
            return 5
        if(partition = "Target Position"):
            return 72960
        raise Exception("Incorrect Partition Name at aggregator")
        
    @inject("TotalMachineTimeSteps")    
    def set_n_machine_time_steps(self, n_machine_time_steps):        
        self._n_machine_time_steps = n_machine_time_steps

    @overrides(MachineDataSpecableVertex.generate_machine_data_specification)
    def generate_machine_data_specification(
            self, spec, placement, machine_graph, routing_info, iptags,
            reverse_iptags, machine_time_step, time_scale_factor):
        self.placement = placement

        # Setup words + 1 for flags + 1 for recording size
        setup_size = constants.SYSTEM_BYTES_REQUIREMENT

        # Reserve SDRAM space for memory areas:
        edges = list(machine_graph.get_edges_ending_at_vertex(self))

        # Create the data regions for hello world
        self._reserve_memory_regions(spec, setup_size, len(edges))

        # write data for the simulation data item
        spec.switch_write_focus(self.DATA_REGIONS.SYSTEM.value)
        spec.write_array(simulation_utilities.get_simulation_header_array(
            self.get_binary_file_name(), machine_time_step,
            time_scale_factor))

        # recording data region
        spec.switch_write_focus(self.DATA_REGIONS.STRING_DATA.value)
        spec.write_array(recording_utilities.get_recording_header_array(
            [self._n_machine_time_steps*12], self._time_between_requests,
            self._n_machine_time_steps*12 + 256, iptags))

        #writing my key
        spec.switch_write_focus(region=self.DATA_REGIONS.TRANSMISSION_DATA.value)
        
        key1 = routing_info.get_first_key_from_partition("Normalisation Value")
        if key1 is None:
            spec.write_value(0)
            spec.write_value(0)
        else:            
            spec.write_value(1)
            spec.write_value(key1)
            
        key2 = routing_info.get_first_key_from_partition("Target Position")
        if key2 is None:
            spec.write_value(0)
            spec.write_value(0)
        else:            
            spec.write_value(1)
            spec.write_value(key2)
        
        #writing particle keys
        spec.switch_write_focus(region=self.DATA_REGIONS.TRANSMISSION_DATA.value)        

        # End-of-Spec:
        spec.end_specification()

    def _reserve_memory_regions(self, spec, system_size, n_particles):
        spec.reserve_memory_region(
            region=self.DATA_REGIONS.SYSTEM.value, size=system_size,
            label='systemInfo')
        spec.reserve_memory_region(
            region=self.DATA_REGIONS.RECORDED_DATA.value,
            size=recording_utilities.get_recording_header_size(1),
            label="Recording")
        spec.reserve_memory_region(
            region=self.DATA_REGIONS.TRANSMISSION_DATA.value,
            size=self.TRANSMISSION_SIZE,
            label="My Key")
        spec.reserve_memory_region(
            region=self.DATA_REGIONS.RECEPTION_BASE_KEYS.value,
            size=n_particles*4,
            label="Particle Keys")

    def read(self, placement, buffer_manager):
        """ Get the data written into sdram

        :param placement: the location of this vertex
        :param buffer_manager: the buffer manager
        :return: string output
        """
        data_pointer, missing_data = buffer_manager.get_data_for_vertex(
            placement, 0)
        if missing_data:
            raise Exception("missing data!")
        record_raw = data_pointer.read_all()
        output = str(record_raw)
        return output

    def get_minimum_buffer_sdram_usage(self):
        return self._string_data_size

    def get_n_timesteps_in_buffer_space(self, buffer_space, machine_time_step):
        return recording_utilities.get_n_timesteps_in_buffer_space(
            buffer_space, len("Hello world"))

    def get_recorded_region_ids(self):
        return [0]

    def get_recording_region_base_address(self, txrx, placement):
        return helpful_functions.locate_memory_region_for_placement(
            placement, self.DATA_REGIONS.STRING_DATA.value, txrx)

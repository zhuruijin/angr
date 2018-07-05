import struct
import logging
import os
import re

from .plugin import SimStatePlugin
from ..errors import SimConcreteRegisterError
from archinfo import ArchX86, ArchAMD64

l = logging.getLogger("state_plugin.concrete")
l.setLevel(logging.DEBUG)


class Concrete(SimStatePlugin):
    def __init__(self, segment_registers_initialized=False, segment_registers_callback_initialized=False,
                 whitelist=[], fs_register_bp=None, synchronize_cle=True, already_sync_objects_addresses=[],
                 ):

        self.segment_registers_initialized = segment_registers_initialized
        self.segment_registers_callback_initialized = segment_registers_callback_initialized

        self.whitelist = whitelist
        self.fs_register_bp = fs_register_bp
        self.synchronize_cle = synchronize_cle
        self.already_sync_objects_addresses = already_sync_objects_addresses

    def copy(self, _memo):
        conc = Concrete(segment_registers_initialized=self.segment_registers_initialized,
                        segment_registers_callback_initialized=self.segment_registers_callback_initialized,
                        whitelist=self.whitelist,
                        fs_register_bp=self.fs_register_bp,
                        synchronize_cle=self.synchronize_cle,
                        already_sync_objects_addresses=self.already_sync_objects_addresses
                        )
        return conc

    def merge(self):
        pass

    def widen(self):
        pass

    def set_state(self, state):
        SimStatePlugin.set_state(self, state)

    def sync(self):
        """
        Handle the switch between the concrete execution and angr.
        This method takes care of:
        1- Synchronize registers.
        2- Set a concrete target to the memory backer so the memory reads are redirected in the concrete process memory.
        3- If possible restore the SimProcedures with the real addresses inside the concrete process.
        4- Set an inspect point to sync the segments register as soon as they are read during the symbolic execution.
        5- Flush all the pages loaded until now.

        :return:
        """

        l.debug("Sync the state with the concrete memory inside the Concrete plugin")

        target = self.state.project.concrete_target

        # Setting a concrete memory backend
        self.state.memory.mem._memory_backer.set_concrete_target(target)

        # Sync Angr registers with the one getting from the concrete target
        # registers that we don't want to concretize.
        l.info("Synchronizing general purpose registers")

        to_sync_register = list(filter(lambda x: x.concrete, self.state.arch.register_list))

        for register in to_sync_register:

            # before let's sync all the subregisters of the current register.
            # sometimes this can be helpful ( i.e. ymmm0 e xmm0 )
            if register.subregisters:
                subregisters_names = map(lambda x: x[0], register.subregisters)
                self._sync_registers(subregisters_names, target)

            # finally let's synchronize the whole register
            self._sync_registers([register.name], target)

        # Synchronize the imported functions addresses (.got, IAT) in the
        # concrete process with ones used in the SimProcedures dictionary
        if self.state.project._should_use_sim_procedures and not self.state.project.loader.main_object.pic:
            l.info("Restoring SimProc using concrete memory")
            for reloc in self.state.project.loader.main_object.relocs:

                if reloc.symbol:  # consider only reloc with a symbol
                    l.debug("Trying to re-hook SimProc %s" % reloc.symbol.name)
                    l.debug("reloc.rebased_addr: %s " % hex(reloc.rebased_addr))
                    func_address = target.read_memory(reloc.rebased_addr, self.state.project.arch.bits / 8)
                    func_address = struct.unpack(self.state.project.arch.struct_fmt(), func_address)[0]
                    l.debug("Function address is now: %s " % hex(func_address))
                    self.state.project.rehook_symbol(func_address, reloc.symbol.name)
        else:
            l.warn("SimProc not restored, you are going to simulate also the code of external libraries!")

        # flush the angr memory in order to synchronize them with the content of the
        # concrete process memory when a read/write to the page is performed
        self.state.memory.flush_pages(self.whitelist)
        l.info("Exiting SimEngineConcrete: simulated address %x concrete address %x "
               % (self.state.addr, target.read_register("pc")))

        # now we have to register a SimInspect in order to synchronize the segments register
        # on demand when the symbolic execution accesses it
        if not self.segment_registers_callback_initialized:
            self.fs_register_bp = self.state.inspect.b('reg_read', reg_read_offset=self.state.project.simos.get_segment_register_name(),
                                                       action=self._sync_segments)

            self.segment_registers_callback_initialized = True

            l.debug("Set SimInspect breakpoint to the new state!")

        if self.synchronize_cle:
            l.debug("Synchronizing CLE backend with the concrete process' memory mapping")

            try:
                vmmap = target.get_mappings()
            except NotImplementedError:
                l.critical("Can't synchronize CLE backend without an implementation of "
                           "the method get_mappings() in the ConcreteTarget.")
                self.synchronize_cle = False
                return

            for mapped_object in self.state.project.loader.all_elf_objects:
                binary_name = os.path.basename(mapped_object.binary)

                # this object has already been sync, skip it.
                if binary_name in self.already_sync_objects_addresses:
                    continue

                for mmap in vmmap:
                    if self._check_mapping_name(binary_name, mmap.name):
                        l.debug("Match! %s -> %s" %(mmap.name, binary_name))

                        # let's make sure that we have the header at this address to confirm that it is the
                        # base address.
                        # That's not a perfect solution, but should work most of the time.
                        result = target.read_memory(mmap.start_address, 10)

                        if self.state.project.simos.get_binary_header_name() in result:
                            if mapped_object.mapped_base == mmap.start_address:
                                # We already have the correct address for this memory mapping
                                l.debug("Object %s is already rebased correctly at 0x%x"
                                        % (binary_name, mapped_object.mapped_base))
                                self.already_sync_objects_addresses.append(mmap.name)
                                break
                            else:
                                # rebase the object if the CLE address doesn't match the real one,
                                # this can happen with PIE binaries and libraries.
                                l.debug("Remapping object %s mapped at address 0x%x at address 0x%x"
                                        % (binary_name, mapped_object.mapped_base, mmap.start_address))
                                mapped_object.mapped_base = mmap.start_address  # Rebase now!
                                self.already_sync_objects_addresses.append(mmap.name)

                                # TODO: sync the symbols if we rebase a library.
                                # Warning: base address is synchronized, but the symbols' relative addresses
                                # refer to the library used during the loading of the binary with CLE.
                                # If the library loaded by CLE during startup and the library used in the concrete
                                # process are different, the absolute addresses of the symbols don't match.

                                break

    def _sync_registers(self, register_names, target):
        for register_name in register_names:
            try:
                reg_value = target.read_register(register_name)
                setattr(self.state.regs, register_name, reg_value)
                l.debug("Register: %s value: %x " % (register_name,
                                                     self.state.se.eval(getattr(self.state.regs, register_name),
                                                                        cast_to=int)))
            except SimConcreteRegisterError as exc:
                l.debug("Can't set register %s reason: %s, if this register is not used "
                        "this message can be ignored" % (register_name, exc))

    def _sync_segments(self, state):
        """
        Segment registers synchronization is on demand as soon as the
        symbolic execution access a segment register.
        """
        target = state.project.concrete_target

        if isinstance(state.arch, ArchAMD64):
            state.project.simos.initialize_segment_register_x64(state, target)
        elif isinstance(state.arch, ArchX86):
            gdt = state.project.simos.initialize_gdt_x86(state, target)
            state.concrete.whitelist.append((gdt.addr, gdt.addr + gdt.limit))

        state.inspect.remove_breakpoint('reg_read', bp=state.concrete.fs_register_bp)
        state.concrete.segment_registers_initialized = True

        state.concrete.fs_register_bp = None

    def _check_mapping_name(self, cle_mapping_name, concrete_mapping_name):

        if cle_mapping_name == concrete_mapping_name:
            return True
        else:
            # removing version and extension information from the library name
            cle_mapping_name = re.findall(r"[\w']+", cle_mapping_name)
            concrete_mapping_name = re.findall(r"[\w']+", concrete_mapping_name)
            if cle_mapping_name[0] == concrete_mapping_name[0]:
                return True
            else:
                return False


from ..sim_state import SimState
SimState.register_default('concrete', Concrete)
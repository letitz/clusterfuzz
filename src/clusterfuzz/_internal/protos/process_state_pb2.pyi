# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
@generated by mypy-protobuf.  Do not edit manually!
isort:skip_file
process_state_proto.proto: A client proto representation of a process,
in a fully-digested state.

Derived from earlier struct and class based models of a client-side
processed minidump found under src/google_breakpad/processor.  The
file process_state.h  holds the top level representation of this model,
supported by additional classes.  We've added a proto representation
to ease serialization and parsing for server-side storage of crash
reports processed on the client.

Author: Jess Gray
"""
import builtins
import collections.abc
import google.protobuf.descriptor
import google.protobuf.internal.containers
import google.protobuf.internal.enum_type_wrapper
import google.protobuf.message
import sys
import typing

if sys.version_info >= (3, 10):
    import typing as typing_extensions
else:
    import typing_extensions

DESCRIPTOR: google.protobuf.descriptor.FileDescriptor

@typing_extensions.final
class ProcessStateProto(google.protobuf.message.Message):
    """package common;

    A proto representation of a process, in a fully-digested state.
    See src/google_breakpad/processor/process_state.h
    Next value: 14
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    @typing_extensions.final
    class Crash(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        REASON_FIELD_NUMBER: builtins.int
        ADDRESS_FIELD_NUMBER: builtins.int
        reason: builtins.str
        """The type of crash.  OS- and possibly CPU- specific.  For example,
        "EXCEPTION_ACCESS_VIOLATION" (Windows), "EXC_BAD_ACCESS /
        KERN_INVALID_ADDRESS" (Mac OS X), "SIGSEGV" (other Unix).
        """
        address: builtins.int
        """If crash_reason implicates memory, the memory address that caused the
        crash.  For data access errors, this will be the data address that
        caused the fault.  For code errors, this will be the address of the
        instruction that caused the fault.
        """
        def __init__(
            self,
            *,
            reason: builtins.str | None = ...,
            address: builtins.int | None = ...,
        ) -> None: ...
        def HasField(self, field_name: typing_extensions.Literal["address", b"address", "reason", b"reason"]) -> builtins.bool: ...
        def ClearField(self, field_name: typing_extensions.Literal["address", b"address", "reason", b"reason"]) -> None: ...

    @typing_extensions.final
    class Thread(google.protobuf.message.Message):
        DESCRIPTOR: google.protobuf.descriptor.Descriptor

        FRAMES_FIELD_NUMBER: builtins.int
        @property
        def frames(self) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[global___StackFrame]:
            """Stack for the given thread"""
        def __init__(
            self,
            *,
            frames: collections.abc.Iterable[global___StackFrame] | None = ...,
        ) -> None: ...
        def ClearField(self, field_name: typing_extensions.Literal["frames", b"frames"]) -> None: ...

    TIME_DATE_STAMP_FIELD_NUMBER: builtins.int
    PROCESS_CREATE_TIME_FIELD_NUMBER: builtins.int
    CRASH_FIELD_NUMBER: builtins.int
    ASSERTION_FIELD_NUMBER: builtins.int
    REQUESTING_THREAD_FIELD_NUMBER: builtins.int
    THREADS_FIELD_NUMBER: builtins.int
    MODULES_FIELD_NUMBER: builtins.int
    OS_FIELD_NUMBER: builtins.int
    OS_SHORT_FIELD_NUMBER: builtins.int
    OS_VERSION_FIELD_NUMBER: builtins.int
    CPU_FIELD_NUMBER: builtins.int
    CPU_INFO_FIELD_NUMBER: builtins.int
    CPU_COUNT_FIELD_NUMBER: builtins.int
    time_date_stamp: builtins.int
    """The time-date stamp of the original minidump (time_t format)"""
    process_create_time: builtins.int
    """The time-date stamp when the process was created (time_t format)"""
    @property
    def crash(self) -> global___ProcessStateProto.Crash: ...
    assertion: builtins.str
    """If there was an assertion that was hit, a textual representation
    of that assertion, possibly including the file and line at which
    it occurred.
    """
    requesting_thread: builtins.int
    """The index of the thread that requested a dump be written in the
    threads vector.  If a dump was produced as a result of a crash, this
    will point to the thread that crashed.  If the dump was produced as
    by user code without crashing, and the dump contains extended Breakpad
    information, this will point to the thread that requested the dump.
    """
    @property
    def threads(self) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[global___ProcessStateProto.Thread]:
        """Stacks for each thread (except possibly the exception handler
        thread) at the time of the crash.
        """
    @property
    def modules(self) -> google.protobuf.internal.containers.RepeatedCompositeFieldContainer[global___CodeModule]:
        """The modules that were loaded into the process represented by the
        ProcessState.
        """
    os: builtins.str
    """System Info: OS and CPU

    A string identifying the operating system, such as "Windows NT",
    "Mac OS X", or "Linux".  If the information is present in the dump but
    its value is unknown, this field will contain a numeric value.  If
    the information is not present in the dump, this field will be empty.
    """
    os_short: builtins.str
    """A short form of the os string, using lowercase letters and no spaces,
    suitable for use in a filesystem.  Possible values are "windows",
    "mac", and "linux".  Empty if the information is not present in the dump
    or if the OS given by the dump is unknown.  The values stored in this
    field should match those used by MinidumpSystemInfo::GetOS.
    """
    os_version: builtins.str
    """A string identifying the version of the operating system, such as
    "5.1.2600 Service Pack 2" or "10.4.8 8L2127".  If the dump does not
    contain this information, this field will be empty.
    """
    cpu: builtins.str
    """A string identifying the basic CPU family, such as "x86" or "ppc".
    If this information is present in the dump but its value is unknown,
    this field will contain a numeric value.  If the information is not
    present in the dump, this field will be empty.  The values stored in
    this field should match those used by MinidumpSystemInfo::GetCPU.
    """
    cpu_info: builtins.str
    """A string further identifying the specific CPU, such as
    "GenuineIntel level 6 model 13 stepping 8".  If the information is not
    present in the dump, or additional identifying information is not
    defined for the CPU family, this field will be empty.
    """
    cpu_count: builtins.int
    """The number of processors in the system.  Will be greater than one for
    multi-core systems.
    """
    def __init__(
        self,
        *,
        time_date_stamp: builtins.int | None = ...,
        process_create_time: builtins.int | None = ...,
        crash: global___ProcessStateProto.Crash | None = ...,
        assertion: builtins.str | None = ...,
        requesting_thread: builtins.int | None = ...,
        threads: collections.abc.Iterable[global___ProcessStateProto.Thread] | None = ...,
        modules: collections.abc.Iterable[global___CodeModule] | None = ...,
        os: builtins.str | None = ...,
        os_short: builtins.str | None = ...,
        os_version: builtins.str | None = ...,
        cpu: builtins.str | None = ...,
        cpu_info: builtins.str | None = ...,
        cpu_count: builtins.int | None = ...,
    ) -> None: ...
    def HasField(self, field_name: typing_extensions.Literal["assertion", b"assertion", "cpu", b"cpu", "cpu_count", b"cpu_count", "cpu_info", b"cpu_info", "crash", b"crash", "os", b"os", "os_short", b"os_short", "os_version", b"os_version", "process_create_time", b"process_create_time", "requesting_thread", b"requesting_thread", "time_date_stamp", b"time_date_stamp"]) -> builtins.bool: ...
    def ClearField(self, field_name: typing_extensions.Literal["assertion", b"assertion", "cpu", b"cpu", "cpu_count", b"cpu_count", "cpu_info", b"cpu_info", "crash", b"crash", "modules", b"modules", "os", b"os", "os_short", b"os_short", "os_version", b"os_version", "process_create_time", b"process_create_time", "requesting_thread", b"requesting_thread", "threads", b"threads", "time_date_stamp", b"time_date_stamp"]) -> None: ...

global___ProcessStateProto = ProcessStateProto

@typing_extensions.final
class StackFrame(google.protobuf.message.Message):
    """Represents a single frame in a stack
    See src/google_breakpad/processor/stack_frame.h
    Next value: 9
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    class _FrameTrust:
        ValueType = typing.NewType("ValueType", builtins.int)
        V: typing_extensions.TypeAlias = ValueType

    class _FrameTrustEnumTypeWrapper(google.protobuf.internal.enum_type_wrapper._EnumTypeWrapper[StackFrame._FrameTrust.ValueType], builtins.type):  # noqa: F821
        DESCRIPTOR: google.protobuf.descriptor.EnumDescriptor
        FRAME_TRUST_NONE: StackFrame._FrameTrust.ValueType  # 0
        """Unknown"""
        FRAME_TRUST_SCAN: StackFrame._FrameTrust.ValueType  # 1
        """Scanned the stack, found this"""
        FRAME_TRUST_CFI_SCAN: StackFrame._FrameTrust.ValueType  # 2
        """Found while scanning stack using call frame"""
        FRAME_TRUST_FP: StackFrame._FrameTrust.ValueType  # 3
        """info
        Derived from frame pointer
        """
        FRAME_TRUST_CFI: StackFrame._FrameTrust.ValueType  # 4
        """Derived from call frame info"""
        FRAME_TRUST_PREWALKED: StackFrame._FrameTrust.ValueType  # 5
        """Explicitly provided by some external stack"""
        FRAME_TRUST_CONTEXT: StackFrame._FrameTrust.ValueType  # 6
        """walker.
        Given as instruction pointer in a context
        """

    class FrameTrust(_FrameTrust, metaclass=_FrameTrustEnumTypeWrapper):
        """Indicates how well the instruction pointer derived during
        stack walking is trusted. Since the stack walker can resort to
        stack scanning, it can wind up with dubious frames.
        In rough order of "trust metric".
        """

    FRAME_TRUST_NONE: StackFrame.FrameTrust.ValueType  # 0
    """Unknown"""
    FRAME_TRUST_SCAN: StackFrame.FrameTrust.ValueType  # 1
    """Scanned the stack, found this"""
    FRAME_TRUST_CFI_SCAN: StackFrame.FrameTrust.ValueType  # 2
    """Found while scanning stack using call frame"""
    FRAME_TRUST_FP: StackFrame.FrameTrust.ValueType  # 3
    """info
    Derived from frame pointer
    """
    FRAME_TRUST_CFI: StackFrame.FrameTrust.ValueType  # 4
    """Derived from call frame info"""
    FRAME_TRUST_PREWALKED: StackFrame.FrameTrust.ValueType  # 5
    """Explicitly provided by some external stack"""
    FRAME_TRUST_CONTEXT: StackFrame.FrameTrust.ValueType  # 6
    """walker.
    Given as instruction pointer in a context
    """

    INSTRUCTION_FIELD_NUMBER: builtins.int
    MODULE_FIELD_NUMBER: builtins.int
    FUNCTION_NAME_FIELD_NUMBER: builtins.int
    FUNCTION_BASE_FIELD_NUMBER: builtins.int
    SOURCE_FILE_NAME_FIELD_NUMBER: builtins.int
    SOURCE_LINE_FIELD_NUMBER: builtins.int
    SOURCE_LINE_BASE_FIELD_NUMBER: builtins.int
    TRUST_FIELD_NUMBER: builtins.int
    instruction: builtins.int
    """The program counter location as an absolute virtual address.  For the
    innermost called frame in a stack, this will be an exact program counter
    or instruction pointer value.  For all other frames, this will be within
    the instruction that caused execution to branch to a called function,
    but may not necessarily point to the exact beginning of that instruction.
    """
    @property
    def module(self) -> global___CodeModule:
        """The module in which the instruction resides."""
    function_name: builtins.str
    """The function name, may be omitted if debug symbols are not available."""
    function_base: builtins.int
    """The start address of the function, may be omitted if debug symbols
    are not available.
    """
    source_file_name: builtins.str
    """The source file name, may be omitted if debug symbols are not available."""
    source_line: builtins.int
    """The (1-based) source line number, may be omitted if debug symbols are
    not available.
    """
    source_line_base: builtins.int
    """The start address of the source line, may be omitted if debug symbols
    are not available.
    """
    trust: global___StackFrame.FrameTrust.ValueType
    """Amount of trust the stack walker has in the instruction pointer
    of this frame.
    """
    def __init__(
        self,
        *,
        instruction: builtins.int | None = ...,
        module: global___CodeModule | None = ...,
        function_name: builtins.str | None = ...,
        function_base: builtins.int | None = ...,
        source_file_name: builtins.str | None = ...,
        source_line: builtins.int | None = ...,
        source_line_base: builtins.int | None = ...,
        trust: global___StackFrame.FrameTrust.ValueType | None = ...,
    ) -> None: ...
    def HasField(self, field_name: typing_extensions.Literal["function_base", b"function_base", "function_name", b"function_name", "instruction", b"instruction", "module", b"module", "source_file_name", b"source_file_name", "source_line", b"source_line", "source_line_base", b"source_line_base", "trust", b"trust"]) -> builtins.bool: ...
    def ClearField(self, field_name: typing_extensions.Literal["function_base", b"function_base", "function_name", b"function_name", "instruction", b"instruction", "module", b"module", "source_file_name", b"source_file_name", "source_line", b"source_line", "source_line_base", b"source_line_base", "trust", b"trust"]) -> None: ...

global___StackFrame = StackFrame

@typing_extensions.final
class CodeModule(google.protobuf.message.Message):
    """Carries information about code modules that are loaded into a process.
    See src/google_breakpad/processor/code_module.h
    Next value: 8
    """

    DESCRIPTOR: google.protobuf.descriptor.Descriptor

    BASE_ADDRESS_FIELD_NUMBER: builtins.int
    SIZE_FIELD_NUMBER: builtins.int
    CODE_FILE_FIELD_NUMBER: builtins.int
    CODE_IDENTIFIER_FIELD_NUMBER: builtins.int
    DEBUG_FILE_FIELD_NUMBER: builtins.int
    DEBUG_IDENTIFIER_FIELD_NUMBER: builtins.int
    VERSION_FIELD_NUMBER: builtins.int
    base_address: builtins.int
    """The base address of this code module as it was loaded by the process."""
    size: builtins.int
    """The size of the code module."""
    code_file: builtins.str
    """The path or file name that the code module was loaded from."""
    code_identifier: builtins.str
    """An identifying string used to discriminate between multiple versions and
    builds of the same code module.  This may contain a uuid, timestamp,
    version number, or any combination of this or other information, in an
    implementation-defined format.
    """
    debug_file: builtins.str
    """The filename containing debugging information associated with the code
    module.  If debugging information is stored in a file separate from the
    code module itself (as is the case when .pdb or .dSYM files are used),
    this will be different from code_file.  If debugging information is
    stored in the code module itself (possibly prior to stripping), this
    will be the same as code_file.
    """
    debug_identifier: builtins.str
    """An identifying string similar to code_identifier, but identifies a
    specific version and build of the associated debug file.  This may be
    the same as code_identifier when the debug_file and code_file are
    identical or when the same identifier is used to identify distinct
    debug and code files.
    """
    version: builtins.str
    """A human-readable representation of the code module's version."""
    def __init__(
        self,
        *,
        base_address: builtins.int | None = ...,
        size: builtins.int | None = ...,
        code_file: builtins.str | None = ...,
        code_identifier: builtins.str | None = ...,
        debug_file: builtins.str | None = ...,
        debug_identifier: builtins.str | None = ...,
        version: builtins.str | None = ...,
    ) -> None: ...
    def HasField(self, field_name: typing_extensions.Literal["base_address", b"base_address", "code_file", b"code_file", "code_identifier", b"code_identifier", "debug_file", b"debug_file", "debug_identifier", b"debug_identifier", "size", b"size", "version", b"version"]) -> builtins.bool: ...
    def ClearField(self, field_name: typing_extensions.Literal["base_address", b"base_address", "code_file", b"code_file", "code_identifier", b"code_identifier", "debug_file", b"debug_file", "debug_identifier", b"debug_identifier", "size", b"size", "version", b"version"]) -> None: ...

global___CodeModule = CodeModule

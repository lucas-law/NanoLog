# Copyright (c) 2016-2018 Stanford University
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

# This script encapsulates all the logic to parse printf-like format strings
# and generate C++ code/files that perform the record/compression/decompression
# routines for log messages using the NanoLog system.

import errno
import json
import os.path
import re

from collections import namedtuple

# Various globals mapping symbolic names to the object/function names in
# the supporting C++ library. This is done so that changes in namespaces don't
# result in large sweeping changes of this file.
RECORD_ENTRY = "NanoLogInternal::Log::UncompressedEntry"
RECORD_PRIMITIVE_FN = "NanoLogInternal::Log::recordPrimitive"

NIBBLE_OBJ = "BufferUtils::TwoNibbles"
LOG_LEVEL_ENUM = "NanoLog::LogLevel"

LOG_LEVEL_GET_FN = "NanoLog::getLogLevel"
ALLOC_FN = "NanoLogInternal::RuntimeLogger::reserveAlloc"
FINISH_ALLOC_FN = "NanoLogInternal::RuntimeLogger::finishAlloc"

PACK_FN = "BufferUtils::pack"
UNPACK_FN = "BufferUtils::unpack"

GENERATED_CODE_NAMESPACE = "GeneratedFunctions"

# This class assigns unique identifiers to unique printf-like format strings,
# generates C++ code to record/compress/decompress the printf-like statements
# in the NanoLog system, and maintains these mappings between multiple
# invocations of the preprocessor system.
#
# This class is intended to be used in two stages. In the first stage, the
# preprocessor component shall identify all the log statements in the user
# files and pass them to the FunctionGenerator, which will attempt to collapse
# similar log statements and return code needed to record the dynamic log
# statement arguments for the preprocessor to inject. The first stage can be
# performed piece-meal since this script maintains state by outputting
# intermediate metadata files to be combined in the second stage. This feature
# is useful for both parallel and partial rebuilds.
#
# In the second stage, after all the user files are processed and compiled,
# the metadata files can be aggregated and passed to this script to output the
# final C++ code required for the compression and decompression of the log
# statements in the runtime library. This stage should be the last step
# before compiling the runtime library and linking it with user code.
class FunctionGenerator(object):

    # Constructor for FunctionGenerator
    def __init__(self):
        # Map of all NANO_LOG statements encountered by this instance of
        # FunctionGenerator. There should be exactly one entry per NANO_LOG
        # in the user sources as each entry is indexed by its format string and
        # the filename/line number of where it occurred. It is pre-populated
        # with an invalid entry to demonstrate the structure of the map
        #
        ### Design Decision ### We uniquely identify log messages by their
        # format strings and filename:linenum so that we can embed more static
        # information (i.e. filename:linenum) at the expense of more entries.
        self.logId2Code = {
            # Mangled form of a NANO_LOG's format string, filename,
            # and line number as generated by generateLogIdStr()
            "__INVALID__INVALID__INVALID__":
                {
                    # printf-like format string in the original NANO_LOG
                    "fmtString"         : "INVALID",

                    # File and line number where NANO_LOG occurred
                    "filename"          : "INVALID.cc",
                    "linenum"           : "-1",

                    # Which g++ preprocessed file did we find this NANO_LOG in?
                    # This can differ from the above in the cases where the
                    # NANO_LOG statement is defined in a header file and it's
                    # include-d in a .cc file. In this case the g++
                    # preprocessor will inline the log statement.
                    "compilationUnit"   : "INVALID.cc",

                    # Complete function definitions for recording, compressing
                    # and decompressing a NANO_LOG statement
                    "recordFnDef"       : "invalidRecord(int arg0) { ... }",
                    "compressFnDef"     : "invalidCompress(...) { ....}",
                    "decompressFnDef"   : "invalidDecompress(...) { ... }",

                    # Function names for the recording, compressing and
                    # decompressing functions above
                    "recordFnName"      : "invalidRecord",
                    "compressFnName"    : "invalidCompress",
                    "decompressFnName"  : "invalidDecompress",

                    # C-style function declaration for record()
                    "recordFnDecl"      : "void invalidRecord(LogLevel, ...)",

                    # Encodes the data structures needed by the decompresor
                    # to interpret the compact log generated by compressFn
                    "dictionaryFragment": "{...}"
                }
        }

        # Debug data structure that keeps track of the number of parameter
        # combinations (i.e. "%d %d") by mapping the format string to a counter
        self.argLists2Cnt = {}

    # Output the internal state of the FunctionGenerator to a JSON file that
    # can later be aggregated to generate the C++ file that contains the
    # compression and decompression functions.
    #
    # \param filename
    #           file to persist the state to
    def outputMappingFile(self, filename):
        dirname = os.path.dirname(filename)
        if dirname and not os.path.exists(dirname):
            try:
                os.makedirs(dirname)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise

        with open(filename, 'w') as json_file:
            outputJSON = {
                "argLists2Cnt":self.argLists2Cnt,
                "logId2Code":self.logId2Code
            }

            json_file.write(json.dumps(outputJSON, sort_keys=True,
                                            indent=4, separators=(',', ': ')))

    # Output the C++ header needed by the runtime library to perform the log
    # compression and decompression routines. The file shall contain the
    # following data structures/code:
    #       - Assignment of extern int's that uniquely id the log statements
    #       - A function array mapping id's to compression functions
    #       - A function array mapping id's to decompression functions
    #       - The supporting compression/decompression functions
    #       - The record function that should have been injected (for debugging)
    #
    # \param filename
    #               The C++ file to emit
    @staticmethod
    def outputCompilationFiles(outputFileName="BufferStuffer.h", inputFiles=[]):
        # Merge all the intermediate compilations
        mergedCode = {}
        for filename in inputFiles:
            with open(filename, 'r') as iFile:
                loaded_json = json.load(iFile)
                mergedCode.update(loaded_json["logId2Code"])

        # Output the C++ code. It may be a bit hard to read admist the static
        # C++ code, but all the code immediately before/after a triple quote
        # sections are in the same indention.
        with open(outputFileName, 'w') as oFile:
            oFile.write("""
#ifndef BUFFER_STUFFER
#define BUFFER_STUFFER

#include "NanoLog.h"
#include "Packer.h"

#include <string>

// Since some of the functions/variables output below are for debugging purposes
// only (i.e. they're not used in their current form), squash all gcc complaints
// about unused variables/functions.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#pragma GCC diagnostic ignored "-Wunused-variable"

/**
 * Describes a log message found in the user sources by the original format
 * string provided, the file where the log message occurred, and the line number
 */
struct LogMetadata {{
  const char *fmtString;
  const char *fileName;
  uint32_t lineNumber;
  {logLevelEnum} logLevel;
}};

// Start an empty namespace to enclose all the record(debug)/compress/decompress
// and support functions
namespace {{

using namespace NanoLog::LogLevels;
""".format(logLevelEnum=LOG_LEVEL_ENUM))
            for logId, code in mergedCode.iteritems():
                if logId == "__INVALID__INVALID__INVALID__":
                    continue

                oFile.write(code["recordFnDef"] + "\n")
                oFile.write(code["compressFnDef"] + "\n")
                oFile.write(code["decompressFnDef"] + "\n")

            oFile.write("""
} // end empty namespace

// Assignment of numerical ids to format NANO_LOG occurrences
""")

            # Here, we take the iteration order as the canonical order
            count = 0
            logId2Metadata = []
            compressFnNameArray = []
            decompressFnNameArray = []
            dictionaryFragments = []
            for logId, code in mergedCode.iteritems():
                if logId == "__INVALID__INVALID__INVALID__":
                    continue

                dictionaryFragments.append(code['dictionaryFragment'])
                logId2Metadata.append("{\"%s\", \"%s\", %d, %s}" % (
                    code["fmtString"],
                    code["filename"],
                    code["linenum"],
                    code["logLevel"]
                ))

                oFile.write("extern const int %s = %d; // %s:%d \"%s\"\n" % (
                        generateIdVariableNameFromLogId(logId),
                        count,
                        code["filename"],
                        code["linenum"],
                        code["fmtString"]
                ))
                count += 1

                compressFnNameArray.append(code["compressFnName"])
                decompressFnNameArray.append(code["decompressFnName"])
            oFile.write("""
// Start new namespace for generated ids and code
namespace {namespace} {{

// Map of numerical ids to log message metadata
struct LogMetadata logId2Metadata[{count}] =
{{
    {listOfLogId2Metadata}
}};

// Map of numerical ids to compression functions
ssize_t
(*compressFnArray[{count}]) ({Entry} *re, char* out)
{{
    {listOfCompressFnNames}
}};

// Map of numerical ids to decompression functions
void
(*decompressAndPrintFnArray[{count}]) (const char **in,
                                        FILE *outputFd,
                                        void (*aggFn)(const char*, ...))
{{
    {listOfDecompressionFnNames}
}};

// Writes the metadata needed by the decompressor to interpret the log messages
// generated by compressFn.
long int writeDictionary(char *buffer, char *endOfBuffer) {{
    using namespace NanoLogInternal::Log;
    char *startPos = buffer;
    {combinedDictionaryFragments}
    return buffer - startPos;
}}

// Total number of logIds. Can be used to bounds check array accesses.
size_t numLogIds = {count};

// Pop the unused gcc warnings
#pragma GCC diagnostic pop

}}; // {namespace}

#endif /* BUFFER_STUFFER */
""".format(count=count,
           Entry=RECORD_ENTRY,
           listOfLogId2Metadata=",\n".join(logId2Metadata),
           listOfCompressFnNames=",\n".join(compressFnNameArray),
           listOfDecompressionFnNames=",\n".join(decompressFnNameArray),
           combinedDictionaryFragments="\n\n".join(dictionaryFragments),
           namespace=GENERATED_CODE_NAMESPACE
))

    # Given a compilation unit via filename, return all the record functions
    # that were generated for that file.
    #
    # \param compilationUnit
    #           filename of compilation unit
    #
    # \return
    #            List of record function definitions related to the compilation
    #            unit
    def getRecordFunctionDefinitionsFor(self, compilationUnit):
        recordFns = []

        for logId, code in self.logId2Code.iteritems():
            if code["compilationUnit"] == compilationUnit:
                recordFns.append(code["recordFnDef"])

        return recordFns

    # Given the format string and the arguments to a log statement, generate
    # the code required to record the dynamic information in the
    # NanoLog system.
    #
    # Note that this function will only return the record function declaration
    # and invocation. The defintion for a compilationUnit can be gotten via
    # getRecordFunctionDefinitionsFor(compilationUnit)
    #
    # \param logLevel
    #           The LogLevel associated with this NANO_LOG statement
    #
    # \param fmtString
    #           C++ printf-like format string for the log message
    #           (note "%n"  is not supported)
    # \param compilationName
    #           C++ file being preprocessed/compiled
    # \param filename
    #           The original file where the log statement occurred. This can
    #           differ from compilationUnit in the cases where log messages
    #           in header files are #include-d/inlined into the compilationUnit
    # \param linenum
    #           The line in the filename where the log statement was found
    #
    # \return
    #           tuple of the record function declaration and invocation.
    #
    # \throws ValueError
    #           If there's is a syntax error in the format string
    def generateLogFunctions(self, logLevel, fmtString,
                                    compilationName, filename, linenum):

        logId = generateLogIdStr(fmtString, filename, linenum)

        # If we've already generated code for this invocation, don't do it again
        if self.logId2Code.get(logId):
            code = self.logId2Code.get(logId)
            return (code.get('recordFnDecl'), code.get('recordFnName'))

        # Get a preprocessed list of the format specifiers in a format string
        fmtSpecifiers = splitAndParseTypesInFmtString(fmtString)

        # Build a list of argument types that the printf-function
        # corresponding to the format string would actually take in.
        argList = []
        for fmtSpecifier in fmtSpecifiers:
            if not fmtSpecifier.type:
                continue

            # In addition to the parameter for the specifier, variable
            # variable width/preicsion requires extra parameters.
            if fmtSpecifier.width == '*':
                argList.append("int")

            if fmtSpecifier.precision == '*':
                argList.append("int")

            argList.append(fmtSpecifier.type)

        functionParametersString = "".join([", %s arg%d" % (type, idx)
                                          for idx, type in enumerate(argList)])

        recordFnName = "__syang0__fl" + logId
        recordDeclaration = "void %s(%s level, const char* fmtStr %s)" % (
                    recordFnName, LOG_LEVEL_ENUM, functionParametersString)

        # Keep track of instance metrics
        if functionParametersString in self.argLists2Cnt:
            self.argLists2Cnt[functionParametersString] += 1
        else:
            self.argLists2Cnt[functionParametersString] = 1


        ###
        # Generate Record function
        ###

        # Create lists identifying which argument indexes are (not) strings
        stringArgsIdx = [idx for idx, fmt in enumerate(argList)
                                                if isStringType(fmt)]
        nonStringArgsIdx = [idx for idx, fmt in enumerate(argList)
                                                if idx not in stringArgsIdx]

        # Create more usable strings for each list
        strlenDeclarations = []
        argNum = 0
        for fmtSpecifier in fmtSpecifiers:
            precision = fmtSpecifier.precision

            if precision == '*':
                argNum += 1

            if fmtSpecifier.width == '*':
                argNum += 1

            if not isStringType(fmtSpecifier.type):
                argNum += 1
                continue

            strlenToAdd = "size_t str{0}Len = ".format(argNum)

            # The +1's below are for the NULL character
            if isWideString(fmtSpecifier.type):
                if not precision:
                    strlenToAdd += "(1 + wcslen(arg{0}))*sizeof(wchar_t);"\
                                        .format(argNum)
                elif precision == '*':
                    strlenToAdd += "(1 + wcsnlen_s(arg{0}, arg{1}))*sizeof(wchar_t);"\
                                        .format(argNum,argNum - 1)
                else:
                    strlenToAdd += "(1 + wcsnlen_s(arg{0}, {1}))*sizeof(wchar_t);"\
                                        .format(argNum, precision)
            else:
                if not precision:
                    strlenToAdd += "1 + strlen(arg{0});".format(argNum)
                elif precision == '*':
                    strlenToAdd += "1 + strnlen(arg{0}, arg{1});".format(argNum,
                                                                     argNum - 1)
                else:
                    strlenToAdd += "1 + strnlen(arg{0}, {1});".format(argNum,
                                                                     precision)
            strlenDeclarations.append(strlenToAdd)

            argNum += 1

        # For these two partial sums, it must end in a '+' character. Also,
        # for stringLenPartialSum, there's a +1 for a NULL character at the end
        stringLenPartialSum = "".join(["str%dLen + " % (idx)
                                      for idx in stringArgsIdx])

        nonStringSizeOfPartialSum = "".join(["sizeof(arg%d) + " % idx
                                          for idx in nonStringArgsIdx])

        # Bytes needed to store the primitive byte lengths
        numNibbles = len(nonStringArgsIdx)
        nibbleByteSizes = ( numNibbles + 1)/2

        recordNonStringArgsCode = "".join(["\t%s(buffer, arg%d);\n" % \
                (RECORD_PRIMITIVE_FN, idx) for idx in nonStringArgsIdx])

        recordStringsArgsCode = ["memcpy(buffer, arg{0}, str{0}Len); "
               "printf(\"memcpying length %lu\\r\\n\", str{0}Len);"
               "buffer += str{0}Len;"
               "*(reinterpret_cast<std::remove_const<typename std::remove_pointer<decltype(arg{0})>::type>::type*>(buffer) - 1) = L'\\0';".format(
                                               idx) for idx in stringArgsIdx]

        # Start Generating the record code
        recordCode = \
"""
inline {function_declaration} {{
    extern const uint32_t {idVariableName};

    if (level > {getLogLevelFn}())
        return;

    uint64_t timestamp = PerfUtils::Cycles::rdtsc();
    {strlen_declaration};
    size_t allocSize = {primitive_size_sum} {strlen_sum} sizeof({entry});
    {entry} *re = reinterpret_cast<{entry}*>({alloc_fn}(allocSize));

    re->fmtId = {idVariableName};
    re->timestamp = timestamp;
    re->entrySize = static_cast<uint32_t>(allocSize);

    char *buffer = re->argData;

    // Record the non-string arguments
    {recordNonStringArgsCode}

    // Record the strings (if any) at the end of the entry
    {recordStringsArgsCode}

    // Make the entry visible
    {finishAlloc_fn}(allocSize);
}}
""".format(function_declaration = recordDeclaration,
       getLogLevelFn=LOG_LEVEL_GET_FN,
       strlen_declaration = "\r\n\t".join(strlenDeclarations),
       primitive_size_sum = nonStringSizeOfPartialSum,
       strlen_sum = stringLenPartialSum,
       entry = RECORD_ENTRY,
       alloc_fn = ALLOC_FN,
       idVariableName = generateIdVariableNameFromLogId(logId),
       nibble_size = nibbleByteSizes,
       recordNonStringArgsCode = recordNonStringArgsCode,
       recordStringsArgsCode = "\r\n\t".join(recordStringsArgsCode),
       finishAlloc_fn = FINISH_ALLOC_FN
)

        ###
        # Generate compression
        ###

        # Generate code to compress the arguments from a RecordEntry to
        # an output array. Note that the compression runtime code should have
        # handled the metadata, so we don't have to worry about that here

        readBackNonStringArgsCode = ""
        for idx in nonStringArgsIdx:
            readBackNonStringArgsCode += \
                "\t{type} arg{id} = *reinterpret_cast<{type}*>(args); " \
                "args +=sizeof({type});\n".format(type=argList[idx], id=idx)

        packNonStringArgsCode = ""
        for i, idx in enumerate(nonStringArgsIdx):
            mem = "first" if (i % 2 == 0) else "second"
            arrIndex = i / 2
            packNonStringArgsCode += \
                "\tnib[%d].%s = 0x0f & static_cast<uint8_t>(%s(&out, arg%d));\n" \
                    % (arrIndex, mem, PACK_FN, idx)

        compressFnName = "compressArgs" + logId
        compressionCode = \
"""
inline ssize_t
{compressFnName}({Entry} *re, char* out) {{
    char *originalOutPtr = out;

    // Allocate nibbles
    {Nibble} *nib = reinterpret_cast<{Nibble}*>(out);
    out += {nibbleBytes};

    char *args = re->argData;

    // Read back all the primitives
    {readBackNonStringArgsCode}

    // Pack all the primitives
    {packNonStringArgsCode}

    if ({hasStrings}) {{
        // memcpy all the strings without compression
        size_t stringBytes = re->entrySize - ({sizeofNonStringTypes} 0)
                                            - sizeof({Entry});
        if (stringBytes > 0) {{
            memcpy(out, args, stringBytes);
            out += stringBytes;
        }}
    }}

    return out - originalOutPtr;
}}
""".format(compressFnName=compressFnName,
        Entry=RECORD_ENTRY,
        Nibble=NIBBLE_OBJ,
        nibbleBytes=nibbleByteSizes,
        readBackNonStringArgsCode=readBackNonStringArgsCode,
        packNonStringArgsCode=packNonStringArgsCode,
        sizeofNonStringTypes=nonStringSizeOfPartialSum,
        hasStrings=("true" if stringArgsIdx else "false")
)

        ###
        # Generate Decompression
        ###

        # Unpack all the non-string arguments with their nibbles
        unpackNonStringArgsCode = ""
        for i, idx in enumerate(nonStringArgsIdx):
            type = argList[idx]
            member = "first" if (i%2 == 0) else "second"

            unpackNonStringArgsCode += "\t%s arg%d = %s<%s>(in, nib[%d].%s);\n" % (
                                        type, idx, UNPACK_FN, type, i/2, member)

        # Read back all the strings
        readbackStringCode = ""
        for idx in stringArgsIdx:
            type = argList[idx]

            strlenFn = "strlen" if not isWideString(type) else "wcslen"
            readbackStringCode += \
            """
                {type} arg{idx} = reinterpret_cast<{type}>(*in);
                (*in) += ({strlenFn}(arg{idx}) + 1)*sizeof(*arg{idx}); // +1 for null terminator
            """.format(idx=idx, type=type, strlenFn=strlenFn)


        decompressFnName = "decompressPrintArgs" + logId
        decompressionCode = \
"""
inline void
{decompressFnName} (const char **in,
                        FILE *outputFd,
                        void (*aggFn)(const char*, ...)) {{
    {Nibble} nib[{nibbleBytes}];
    memcpy(&nib, (*in), {nibbleBytes});
    (*in) += {nibbleBytes};

    // Unpack all the non-string argments
    {unpackNonStringArgsCode}

    // Find all the strings
    {readbackStringCode}

    const char *fmtString = "{fmtString}";
    const char *filename = "{filename}";
    const int linenum = {linenum};
    const {logLevelEnum} logLevel = {logLevel};

    if (outputFd)
        fprintf(outputFd, "{fmtString}" "\\r\\n" {printfArgs});

    if (aggFn)
        (*aggFn)("{fmtString}" {printfArgs});
}}
""".format(decompressFnName=decompressFnName,
        Nibble=NIBBLE_OBJ,
        nibbleBytes=nibbleByteSizes,
        unpackNonStringArgsCode=unpackNonStringArgsCode,
        readbackStringCode=readbackStringCode,
        fmtString=fmtString,
        filename=filename,
        linenum=linenum,
        logLevelEnum=LOG_LEVEL_ENUM,
        logLevel=logLevel,
        printfArgs="".join([", arg%d" % i for i, type in enumerate(argList)])
)
        dictionaryFragment = """
{{
    // {filename}:{linenum} - "{fmtString}"
    FormatMetadata *fm;
    PrintFragment *pf;
    if (buffer + sizeof(FormatMetadata) + {filenameLength} >= endOfBuffer)
        return -1;

    fm = reinterpret_cast<FormatMetadata*>(buffer);
    buffer += sizeof(FormatMetadata);

    fm->numNibbles = {numNibbles};
    fm->numPrintFragments = {numPrintFragments};
    fm->logLevel = {logLevel};
    fm->lineNumber = {linenum};
    fm->filenameLength = {filenameLength};

    buffer = stpcpy(buffer, "{filename}") + 1;
""".format(numNibbles=numNibbles,
           numPrintFragments=len(fmtSpecifiers),
           logLevel=logLevel,
           linenum=linenum,
           filenameLength=len(filename) + 1,
           filename=filename,
           fmtString=fmtString
           )

        count = 0
        for (type, width, precision, substring) in fmtSpecifiers:
            if type:
                enumType = type.replace(" ", "_") + "_t"
                enumType = enumType.replace("*", "_ptr")
            else:
                enumType = "NONE"

            dictionaryFragment += """
            // Fragment {count}
            if (buffer + sizeof(PrintFragment)
                        + sizeof("{substring}")/sizeof(char) >= endOfBuffer)
                return -1;

            pf = reinterpret_cast<PrintFragment*>(buffer);
            buffer += sizeof(PrintFragment);

            pf->argType = {type};
            pf->hasDynamicWidth = {width};
            pf->hasDynamicPrecision = {precision};
            pf->fragmentLength = sizeof("{substring}")/sizeof(char);

            buffer = stpcpy(buffer, "{substring}") + 1;
""".format(count=count,
           type=enumType,
           width="true" if width == '*' else "false",
           precision="true" if precision == '*' else "false",
           substring=substring
            )
            count += 1

        dictionaryFragment += "}\r\n\r\n"

        # All the code has been generated,  save them in our data structure
        code = {
            "fmtString"         : fmtString,
            "filename"          : filename,
            "linenum"           : linenum,
            "logLevel"          : logLevel,
            "compilationUnit"   : compilationName,
            "recordFnDef"       : recordCode,
            "compressFnDef"     : compressionCode,
            "decompressFnDef"   : decompressionCode,
            "recordFnName"      : recordFnName,
            "compressFnName"    : compressFnName,
            "decompressFnName"  : decompressFnName,
            "recordFnDecl"      : recordDeclaration,
            "dictionaryFragment": dictionaryFragment
        }

        self.logId2Code[logId] = code

        return (recordDeclaration, recordFnName)

# Identifies a format specifier's C++ type and optional width/precision in a
# format string. The width/precision could be None, a number, or '*' which
# indicates a dynamic argument. The substring is the portion of the format
# string leading up to and including  this specifier from the last specifier
# or beginning of the string.
FmtType = namedtuple('FmtType', ['type', 'width', 'precision', 'substring'])

# Given a C++ printf-like format string, split the string such that there's
# a) At most one format specifier per substring and
# b) Identify the C-type and width/precision associated w/ that format specifier
#
#
# Note that the "%n" specifier is not supported in the NanoLog system and
# will cause the following function to throw a ValueError
#
# \param fmtString
#           Printf-like format string such as "number=%d, float=%0.2f"
#
# \return
#           A list of FmtType named tuples indicating (C++ type, precision) Ex:
#           [("int", None), ("const char*", "*"), ("const char*", "4"), ...]
#           Note: a precision of '*' indicates previous element is precision
#
# \throws ValueError
#           Thrown if the format string does not conform to standards
def splitAndParseTypesInFmtString(fmtString):
    # This function follows the standard according to the cplusplus reference
    # http://www.cplusplus.com/reference/cstdio/printf/ (9/7/16)

    # These are the set of characters that can serve as specifiers
    signedSet = 'di'
    unsignedSet = 'uoxX'
    floatSet = 'fFeEgGaA'
    integerSet = signedSet + unsignedSet

    # The next while loop scans through the string looking for unescaped "%"
    matches = []
    charIndex = 0
    consecutivePercents = 0
    startOfNextSpecifierSubstring = 0
    while charIndex < len(fmtString):
        c = fmtString[charIndex]

        if c == "\\":
            # Skip the next character if there's an escape
            charIndex += 1
        elif c == "%":
            consecutivePercents += 1
            if consecutivePercents % 2 == 1:
                # At this point we should be at a %, so try to regex it
                match = re.match("^%"
                                 "(?P<flags>[-+ #0]+)?"
                                 "(?P<width>[\\d]+|\\*)?"
                                 "(\\.(?P<precision>\\d+|\\*))?"
                                 "(?P<length>hh|h|l|ll|j|z|Z|t|L)?"
                                 "(?P<specifier>[diuoxXfFeEgGaAcspn])",
                                 fmtString[charIndex:])

                if match:
                    endPos = charIndex + len(match.group(0))
                    substring = fmtString[startOfNextSpecifierSubstring:endPos]
                    startOfNextSpecifierSubstring = endPos

                    matches.append((match, substring))
                elif not re.match("%%", fmtString[charIndex:]):
                    raise ValueError("Unrecognized Format Specifier: \"%s\"" %
                                        fmtString[charIndex:].split()[0])
        else:
            consecutivePercents = 0

        charIndex += 1

    # Fold in the remainder of the format string into the last argument if it
    # exists; otherwise just return our format-less string
    if len(matches) > 0:
        lastItem = matches.pop()
        matches.append((lastItem[0],
                        lastItem[1] + fmtString[startOfNextSpecifierSubstring:]))
    else:
        return [FmtType(None, None, None, fmtString)]

    types = []
    for (fmt, substring) in matches:
        length = fmt.group('length')
        specifier = fmt.group('specifier')
        precision = fmt.group("precision")
        if precision and precision != '*':
            precision = int(float(precision))
        width = fmt.group('width')
        if width and width != '*':
            width = int(float(width))

        # Handle the most common case of regular integer types first
        if specifier in integerSet:
            type = ""

            if specifier in unsignedSet:
                type += "unsigned "

            if not length:
                type += "int "
            elif length == "hh":
                type = "unsigned char" if type else "signed char"
            elif length == "h":
                type += "short int "
            elif length == "l":
                type += "long int"
            elif length == "ll":
                type += "long long int"
            # Special length modifiers here override the original signed-ness
            elif length == "j":
                if specifier in signedSet:
                    type = "intmax_t"
                else:
                    type = "uintmax_t"
            elif length == "z" or length == "Z":
                type = "size_t"
            elif length == "t":
                type = "ptrdiff_t"
            else:
                raise ValueError("Invalid arguments for format specifier "
                                    + fmt.group())

            types.append(FmtType(type.strip(), width, precision, substring))

        # Next are doubles
        elif specifier in floatSet:
            if length == 'L':
                types.append(FmtType("long double", width, precision, substring))
            else:
                types.append(FmtType("double", width, precision, substring))

        # Finally the special cases
        elif specifier == "p":
            if not length:
                types.append(FmtType("const void*", width, precision, substring))
            else:
                raise ValueError("Invalid arguments for format specifier "
                                    + fmt.group())
        elif specifier == "s":
            if not length:
                types.append(FmtType("const char*", width, precision, substring))
            elif length == "l":
                types.append(FmtType("const wchar_t*", width, precision, substring))
            else:
                raise ValueError("Invalid arguments for format specifier "
                                    + fmt.group())
        elif specifier == "c":
            if not length:
                types.append(FmtType("int", width, precision, substring))
            elif length == "l":
                types.append(FmtType("wint_t", width, precision, substring))
            else:
                raise ValueError("Invalid arguments for format specifier "
                                 + fmt.group())
        elif specifier == "n":
            raise ValueError("\"%n\" print specifier not supported in "
                             + fmt.group())

    return types

# Given a C++ type (such as 'int') as identified by parseTypesInFmtString,
# determine whether that type is a string or not.
#
# \param typeStr - Whether a FmtType is a string or not in C/C++ land
def isStringType(typeStr):
    return typeStr and (
            -1 != typeStr.find("char*") or
            -1 != typeStr.find("wchar_t*"))

# Given a C++ type (such as 'int') as identified by parseTypesInFmtString,
# determine whether that type is a wide string or not.
#
# \param typeStr - Whether a FmtType is a string or not in C/C++ land
def isWideString(typeStr):
    return typeStr and -1 != typeStr.find("wchar_t*")

# Helper functions to generate variable names
def generateIdVariableNameFromLogId(logId):
    return "__fmtId" + logId

def generateLogIdStr(fmtString, filename, linenum):
    def encode(string):
        return "".join([c if c.isalnum() else str(ord(c)) for c in string])

    return "__%s__%s__%d__" % (encode(fmtString), encode(filename), linenum)

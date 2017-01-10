/* Copyright (c) 2016 Stanford University
 *
 * Permission to use, copy, modify, and distribute this software for any
 * purpose with or without fee is hereby granted, provided that the above
 * copyright notice and this permission notice appear in all copies.
 *
 * THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
 * WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
 * ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
 * WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
 * ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
 * OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
 */

#include <cstdlib>
#include <fstream>

#include "BufferUtils.h"
#include "Cycles.h"

// File generated by the FastLogger preprocessor that contains all the
// compression and decompression functions.
#include "BufferStuffer.h"

using namespace BufferUtils;

/**
 * Simple program to decompress log files produced by the FastLogger System.
 * Note that this executable must be compiled with the same BufferStuffer.h
 * as the LogCompressor that generated the compressedLog for this to work.
 */
int main(int argc, char** argv) {
    uint32_t bufferSize = 1<<26;

    if (argc < 2) {
        printf("Decompresses log files produced by the FastLogger System\r\n"
                "into a human readable format.\r\n\r\n");
        printf("\tUsage: %s <logFile> [# messages to print]", argv[0]);
        exit(1);
    }

    char *scratchBufferSpace = static_cast<char*>(calloc(1, bufferSize));
    if (!scratchBufferSpace) {
        printf("Malloc of a %d byte array as a staging buffer "
                "for decompressing failed\r\n", bufferSize);
        exit(-1);
    }

    int msgsToPrint = 0;
    if (argc > 2) {
        try {
            msgsToPrint = std::stoi(argv[2]);
        } catch (const std::invalid_argument& e) {
            printf("Invalid # of message to print, please enter a number:"
                    " %s\r\n",  argv[2]);
            exit(-1);
        } catch (const std::out_of_range& e) {
            printf("# of messages to print is too large: %s\r\n", argv[2]);
            printf("If you intend to print all message, "
                    "exclude the # messages to print parameter.\r\n");
            exit(-1);
        }

        if (msgsToPrint < 0) {
            printf("# of messages to print must be positive: %s\r\n", argv[2]);
            exit(-1);
        }
    }

    std::ifstream in(argv[1], std::ifstream::binary);
    if (!in.is_open()) {
        printf("Unable to open file: %s\r\n", argv[1]);
        exit(-1);
    }

    printf("Opening file %s\r\n", argv[1]);

    int linesPrinted = 0;
    uint32_t lastFmtId = 0;
    uint64_t lastTimestamp = 0;
    while (!in.eof()) {
        if (msgsToPrint > 0 && linesPrinted >= msgsToPrint)
            break;

        EntryType nextType = BufferUtils::peekEntryType(in);

        if (nextType == EntryType::LOG_MSG) {
            DecompressedMetadata dm =
                BufferUtils::decompressMetadata(in, lastFmtId, lastTimestamp);
            //TODO(syang0) use cyclesPerSec given in checkpoint
            printf("%4d) +%10.2lf ns: ", linesPrinted, 1.0e9*PerfUtils::Cycles::toSeconds(dm.timestamp - lastTimestamp));
//            printf("+%0.2lf ns\t: ", 1.0e9*PerfUtils::Cycles::toSeconds(dm.timestamp - lastTimestamp));

            decompressAndPrintFnArray[dm.fmtId](in);

            lastFmtId = dm.fmtId;
            lastTimestamp = dm.timestamp;
            ++linesPrinted;
        } else if (nextType == EntryType::CHECKPOINT) {
            // Read in the rest of the checkpoint and don't process (for now)
            Checkpoint cp = BufferUtils::readCheckpoint(in);
            printf("Found a checkpoint. CyclesPerSec=%lf\r\n", cp.cyclesPerSecond);
        } else if (nextType == EntryType::INVALID) {
            // It's possible we hit a pad byte, double check.
            while(in.peek() == 0 && in.good())
                in.get();
        } else {
            printf("Entry type read in metadata does not match anything "
                            "(%d); exiting...\r\n", nextType);
            exit(-1);
        }
    }

    printf("\r\n\r\nDecompression Complete after printing %d log messages\r\n",
            linesPrinted);

    return 0;
}


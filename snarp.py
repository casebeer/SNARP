#!/usr/bin/env python
#
# snarp - Simple Noise Activated Recording in Python
#
# Copyright (C) 2013  Christopher Casebeer <christopher@chc.name>
# Copyright (C) 2011  Merlijn Wajer <merlijn@wizzup.org>
# Copyright (C) 2011  Antonio Ospite <ospite@studenti.unina.it>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
logging.basicConfig(level=logging.DEBUG)

import wave
import time

import struct
import sys
import argparse
import contextlib
import itertools
import collections

SILENCE_MIN = 120
SILENCE_MAX = 135

# Global vars for Wave format metadata
# Usually these are fixed by the format (little, 8 bit -> unsigned, > 8 bit signed), 
# but change these values to override
INPUT_ENDIANNESS = 'little' # Wave format default
INPUT_SIGNEDNESS = None # None means permit Wave format rules to prevail

CHUNK_SECONDS = 1.0 # seconds per silence analysis period


# Context managers for global Wave format overrides
@contextlib.contextmanager
def input_endianness(val):
    assert(val in ('little', 'big'))
    global INPUT_ENDIANNESS
    previous = INPUT_ENDIANNESS
    INPUT_ENDIANNESS = val
    yield
    INPUT_ENDIANNESS = previous

@contextlib.contextmanager
def input_signedness(val):
    assert(val in (None, 'signed', 'unsigned'))
    global INPUT_SIGNEDNESS
    previous = INPUT_SIGNEDNESS
    INPUT_SIGNEDNESS = val
    yield
    INPUT_SIGNEDNESS = previous

@contextlib.contextmanager
def silence_limits(min_, max_):
    '''Override SILENCE_MIN and SILENCE_MAX globals.'''
    global SILENCE_MIN, SILENCE_MAX
    old_min, old_max = SILENCE_MIN, SILENCE_MAX
    SILENCE_MIN, SILENCE_MAX = min_, max_
    yield
    SILENCE_MIN, SILENCE_MAX = old_min, old_max

class NoiseFilter(object):
    def __init__(self):
        pass

class RingBuffer(collections.deque):
    def append(self, value):
        discard = None
        if len(self) == self.maxlen:
            discard = self.popleft()
        collections.deque.append(self, value)
        return discard

def audible_chunks(tagged_segments):
    '''
    Generator producing audio chunks only for audible segments
    '''
    return itertools.imap(lambda pair: pair[1], 
        itertools.ifilter(lambda pair: not pair[0], tagged_segments)
    )

def audible_segments(tagged_segments):
    '''
    Return generator of generators, one per audible segment

    For each audible segment in the chunk stream, return a generator
    for its audio data chunks.
    '''
    return (segment for silent, segment in segmenter(tagged_segments) if not silent)

def segmenter(tagged_segments):
    '''
    Return generator of generators, one per segment

    For each segment in the chunk stream, return a tuple of:
        (segment_is_silent, chunk_generator_for_segment)
    '''
    for silent, segment in itertools.groupby(tagged_segments, key=lambda pair: pair[0]):
        yield silent, itertools.imap(lambda pair: pair[1], segment)

def tag_segments(tagged_chunks):
    '''
    Generator returning chunk frames tagged by segment

    Returns a tuple of (chunk_in_silent_segment, chunk_frames) for each
    chunk in the provided generator. 

    Note that this does not tag based strictly on whether the current chunk is silent
    or audible; rather it tags *which type of segment* the chunk belongs to. 
    '''
    pre_roll_buffer = RingBuffer(maxlen=1)
    segment_silent = True
    for chunk_silent, chunk_samples, chunk_frames in tagged_chunks:
        # four cases: changing state/not changing state X silent chunk/audible chunk
        if chunk_silent != segment_silent:
            if not chunk_silent:
                # starting audible segment, write out pre-roll buffer
                logging.debug("Pre-rolling...")
                for frames in pre_roll_buffer:
                    yield False, frames
                pre_roll_buffer.clear()
            else:
                # ending audible segment, write out any currently consumed chunks
                logging.debug("Post-rolling...")
            yield False, chunk_frames
            segment_silent = chunk_silent
        else: # not changing state
            if chunk_silent:
                frames = pre_roll_buffer.append(chunk_frames)
                if frames is not None:
                    yield True, frames
            else:
                yield False, chunk_frames

def tag_chunks(chunk_gen):
    '''
    Tag each chunk in the generator as silent (True) or audible (False)

    Returns tuple of (chunk_silent, chunk_samples, chunk_frames)
    '''
    for chunk_samples, chunk_frames in chunk_gen:
        if len(chunk_samples) == 0:
            raise EOFError

        min_, max_ = min(chunk_samples), max(chunk_samples)
        audible = min_ < SILENCE_MIN and SILENCE_MAX < max_
        silence = not audible

        logging.debug('min: {0} max: {1} silence: {2}'.format(min_, max_, silence))

        yield silence, chunk_samples, chunk_frames

def chunked_samples(input_wave, chunk_seconds):
    '''
    Generator returning parsed and raw wave data one chunk at a time
    
    Yield a pair of (parsed wave samples, raw wave frames) from `input_wave` 
    in chunks of at most `chunk_seconds` of data. The actual number of frames 
    per chunk will vary with the input wave's frame rate. 
    '''
    sample_width = input_wave.getsampwidth()
    nchannels = input_wave.getnchannels()
    signed_data = input_is_signed_data(input_wave)
    frames_per_chunk = int(input_wave.getframerate() * chunk_seconds)
    while True:
        frames = input_wave.readframes(frames_per_chunk)
        yield list(parse_frames(frames, sample_width, nchannels, signed_data)), frames

def parse_frames(frames, sample_width, nchannels, signed_data):
    '''
    Generator to convert wave frames to sample data
    
    Arguments:
    frames        frame data 
    sample_width  sample width in bytes
    nchannels     number of channels per frame
    signed_data   True if wave data is signed, false if unsigned
    '''
    # todo: coalesce samples for all channels into one?
    chunk_size = sample_width * nchannels
    for i in xrange(0, len(frames), chunk_size):
        frame = frames[i:i + chunk_size]
        yield frame_to_sample(frame, sample_width, signed_data)

def frame_to_sample(frame, sample_width, signed_data):
    '''
    Convert one frame to one sample

    Note that we expect that the frame data will contain frames for all
    channels of the wave file. However, only the first channel will be 
    processed. The data from the remaining channels will be ignored!

    Arguments:
    frame         frame data for *all channels*, only first will be used
    sample_width  sample width in bytes
    signed_data   True if wave data is signed, false if unsigned
    '''
    # handling only the first channel
    frame_data = frame[0:sample_width]

    # Padding all samples to 4byte integer
    if sample_width < 4:

        if INPUT_ENDIANNESS == 'little':
            frame_data_MSB = frame_data[sample_width - 1]
        else:
            frame_data_MSB = frame_data[0]

        # Check if positive or negative and set the MSB accordigly
        if ord(frame_data_MSB) & 0x80:
            padding_MSB = '\xff'
            frame_data_MSB = chr(ord(frame_data_MSB) & ~0x80)
        else:
            padding_MSB = '\x00'

        # Set the middle padding
        padding = '\x00' * (4 - sample_width - 1)

        if INPUT_ENDIANNESS == 'little':
            try:
                frame_data = frame_data + padding + padding_MSB
            except Exception,e:
                import pdb
                pdb.set_trace()
        else:
            frame_data = padding_MSB + padding + frame_data

    fmt = ''
    if INPUT_ENDIANNESS == 'little':
        fmt += '<'
    else:
        fmt += '>'

    if signed_data:
        fmt += 'l'
    else:
        fmt += 'L'

    sample = struct.unpack(fmt, frame_data)
    return sample[0]

def input_is_signed_data(wave_file):
    '''
    Return True if the input contains signed date, False if unsigned

    Wave spec says:
    - sample width == 8 bit ==> unsigned, 
    - sample width > 8 bit ==> signed

    Implement this but permit override if INPUT_SIGNEDNESS global
    is explicitly set. 
    '''
    if INPUT_SIGNEDNESS is None:
        # no explicit override, follow spec
        return (wave_file.getsampwidth() > 1)
    else:
        return INPUT_SIGNEDNESS == 'signed'

def remove_silences(input_file, output_file, bypass_file=None):
    input_wave = wave.open(input_file)

    output_wave = wave.open(output_file, 'wb')
    output_wave.setparams((
        input_wave.getnchannels(),
        input_wave.getsampwidth(),
        input_wave.getframerate(),
        0,
        'NONE',
        'not compressed'
    ))

    bypass_wave = None
    if bypass_file is not None:
        bypass_wave = wave.open(bypass_file, 'wb')
        bypass_wave.setparams((
            input_wave.getnchannels(),
            input_wave.getsampwidth(),
            input_wave.getframerate(),
            0,
            'NONE',
            'not compressed'
        ))

    # Print audio setup
    logging.debug('Input wave params: {0}'.format(input_wave.getparams()))
    logging.debug('Frame rate: {0} Hz'.format(input_wave.getframerate()))


    try:
        for silent_segment, segment in \
            segmenter(
                tag_segments(
                    tag_chunks(
                        chunked_samples(input_wave, CHUNK_SECONDS)
                    )
                )
            ):

            if silent_segment:
                if bypass_wave is not None:
                    for chunk_frames in segment:
                        bypass_wave.writeframes(chunk_frames)
            else:
                for chunk_frames in segment:
                    output_wave.writeframes(chunk_frames)
                    if bypass_wave is not None:
                        bypass_wave.writeframes(chunk_frames)

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        input_wave.close()
        output_wave.close()
        if bypass_wave is not None:
            bypass_wave.close()

def main(*argv):
    parser = argparse.ArgumentParser(description='Remove silence from wave audio data.')
    parser.add_argument(
        '-i',
        '--input_filename', 
        default='-',
        help='Filename to read. Defaults to - for STDIN.'
    )
    parser.add_argument(
        '-b',
        '--bypass_filename',
        default=None,
        help='Filename to write bypass audio to. All audio data read from the input will be passed through.'
    )
    parser.add_argument(
        'output_filename', 
        help='Filename to write to.'
    )
    parser.add_argument(
        '--silence-min',
        type=int,
        default=SILENCE_MIN,
        help='Minimum wave sample value to consider silence.'
    )
    parser.add_argument(
        '--silence-max',
        type=int,
        default=SILENCE_MAX,
        help='Maximum wave sample value to consider silence.'
    )
    parser.add_argument(
        '--input-big-endian',
        action='store_true',
        help='Ignore the Wave spec and interpret input as big endian. Not recommended.'
    )
    parser.add_argument(
        '--input-override-signedness',
        choices=("unsigned", "signed"),
        default=None,
        help='Ignore the Wave spec and interpres input with given signedness. Not recommended.'
    )
    args = parser.parse_args(argv[1:])

    input_filename = args.input_filename
    output_filename = args.output_filename

    input_file = sys.stdin if input_filename == '-' else open(input_filename, 'rb')

    bypass_file = None
    if args.bypass_filename is not None:
        bypass_file = open(args.bypass_filename, 'wb')

    with silence_limits(args.silence_min, args.silence_max):
        with input_endianness('big' if args.input_big_endian else 'little'):
            with input_signedness(args.input_override_signedness):
                with open(output_filename, 'wb') as output_file:
                    remove_silences(input_file, output_file, bypass_file)

    return 0

if __name__ == '__main__':
    sys.exit(main(*sys.argv))


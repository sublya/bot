# Word timings

Karaoke subtitles look broken when a word lights up a quarter of a second early or late,
and every recogniser is off by about that much. Sublya doesn't trust recognised timings
as they are: it moves them to the pauses in the audio, which ffmpeg measures to the
millisecond.

## Pauses

`silencedetect=n=-35dB:d=0.25` gives the pauses: stretches quieter than -35 dB and longer than
a quarter of a second. Children's and amateur speech is full of them, and each one ends
exactly where a word begins.

## Two kinds of recognised timings

**Spans** come from the Whisper API (`timestamp_granularities[]=word`): every word has a start
and an end, and the words are laid end to end. Whisper doesn't know about silence, so a word
that follows a pause usually starts inside that pause, a few hundred milliseconds early.
The rule: if a pause covers the start of a word (or begins within 0.2 s after it) and ends
inside the word, the word starts at the end of that pause. Words without such a pause are
moved 0.2 s later.

**Marks** come from whisper.cpp with DTW token timestamps: one moment per word, a little after
it starts sounding. The rule: if a pause ends between the marks of two words, the later word
starts at the end of that pause; otherwise the mark is moved 0.15 s earlier. The prototype
used this, and the test fixture keeps it as the reference.

Both offsets are the defaults of `STT_LAG`. They were fitted on
`tests/fixtures/natasha`, a 55-second recitation checked frame by frame: after snapping,
Whisper API timings are 0.08 s from the reference on average and never more than 0.5 s.

A word ends where the first pause after it begins, or where the next word starts.

## Reference text

When the user sends the exact text, `difflib.SequenceMatcher` lines it up with what was heard,
comparing lower-cased words without punctuation and with ё read as е:

- matching words keep the heard timings and take the reference spelling;
- a run of replaced words shares the time of what was heard there, in proportion to word
  length;
- heard words missing from the reference are dropped (a stray "и", a hallucinated
  "Продолжение следует...");
- reference words that weren't heard take the gap before the next word, or the tail of the
  previous one;
- a lone dash sticks to the previous word;
- a line break ends the screen.

## Screens

Up to 3 words and 24 characters per screen. A new screen also starts after a pause longer than
0.6 s, after `.?!…` and at the end of a reference line. Each word is one ASS event and stays lit
until the next word starts, so the screen never blinks.

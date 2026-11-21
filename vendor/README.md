# FT8 decoder sources

`ft8_lib` is vendored from https://github.com/kgoba/ft8_lib at the commit in
`ft8_lib/UPSTREAM_COMMIT`. Decoder, encoder, message parsing, FFT and monitor sources are built. The MIT license is retained, together
with the KISS FFT BSD-3-Clause attribution and license.

`native/ft8_decode.c` adapts upstream's `demo/decode_ft8.c` to receive 12 kHz
mono S16_LE PCM on standard input and print time offset, audio Hz and message
as TSV. Changes remove transmission/WAV dependencies and the upstream sync
score presented as an SNR estimate, reject unpack failures and bound the
message table. The separate `native/ft8_encode.c` frontend adapts upstream `demo/gen_ft8.c` GFSK synthesis for standard FT8 messages and outputs 12 or 48 kHz mono S16_LE PCM. Transmission is initiated explicitly by the user. Spectral SNR estimation is implemented in the decode frontend, rather than reporting the sync score as SNR.

The regression WAV and companion decode listing in `tests/fixtures/ft8` come
from the same upstream repository's `test/wav` directory. Four messages are
expected from this lightweight decoder; the reference listing contains five.

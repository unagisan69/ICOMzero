/* GFSK synthesis adapted from kgoba/ft8_lib, MIT, Kārlis Goba. */
#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <math.h>
#include <ft8/message.h>
#include <ft8/encode.h>
#define GFSK_CONST_K 5.336446f
#define LOG(a, ...) ((void)0)
void gfsk_pulse(int n_spsym, float symbol_bt, float* pulse)
{
    for (int i = 0; i < 3 * n_spsym; ++i)
    {
        float t = i / (float)n_spsym - 1.5f;
        float arg1 = GFSK_CONST_K * symbol_bt * (t + 0.5f);
        float arg2 = GFSK_CONST_K * symbol_bt * (t - 0.5f);
        pulse[i] = (erff(arg1) - erff(arg2)) / 2;
    }
}

/// Synthesize waveform data using GFSK phase shaping.
/// The output waveform will contain n_sym symbols.
/// @param[in] symbols Array of symbols (tones) (0-7 for FT8)
/// @param[in] n_sym Number of symbols in the symbol array
/// @param[in] f0 Audio frequency in Hertz for the symbol 0 (base frequency)
/// @param[in] symbol_bt Symbol smoothing filter bandwidth (2 for FT8, 1 for FT4)
/// @param[in] symbol_period Symbol period (duration), seconds
/// @param[in] signal_rate Sample rate of synthesized signal, Hertz
/// @param[out] signal Output array of signal waveform samples (should have space for n_sym*n_spsym samples)
///
void synth_gfsk(const uint8_t* symbols, int n_sym, float f0, float symbol_bt, float symbol_period, int signal_rate, float* signal)
{
    int n_spsym = (int)(0.5f + signal_rate * symbol_period); // Samples per symbol
    int n_wave = n_sym * n_spsym;                            // Number of output samples
    float hmod = 1.0f;

    LOG(LOG_DEBUG, "n_spsym = %d\n", n_spsym);
    // Compute the smoothed frequency waveform.
    // Length = (nsym+2)*n_spsym samples, first and last symbols extended
    float dphi_peak = 2 * M_PI * hmod / n_spsym;
    float dphi[n_wave + 2 * n_spsym];

    // Shift frequency up by f0
    for (int i = 0; i < n_wave + 2 * n_spsym; ++i)
    {
        dphi[i] = 2 * M_PI * f0 / signal_rate;
    }

    float pulse[3 * n_spsym];
    gfsk_pulse(n_spsym, symbol_bt, pulse);

    for (int i = 0; i < n_sym; ++i)
    {
        int ib = i * n_spsym;
        for (int j = 0; j < 3 * n_spsym; ++j)
        {
            dphi[j + ib] += dphi_peak * symbols[i] * pulse[j];
        }
    }

    // Add dummy symbols at beginning and end with tone values equal to 1st and last symbol, respectively
    for (int j = 0; j < 2 * n_spsym; ++j)
    {
        dphi[j] += dphi_peak * pulse[j + n_spsym] * symbols[0];
        dphi[j + n_sym * n_spsym] += dphi_peak * pulse[j] * symbols[n_sym - 1];
    }

    // Calculate and insert the audio waveform
    float phi = 0;
    for (int k = 0; k < n_wave; ++k)
    { // Don't include dummy symbols
        signal[k] = sinf(phi);
        phi = fmodf(phi + dphi[k + n_spsym], 2 * M_PI);
    }

    // Apply envelope shaping to the first and last symbols
    int n_ramp = n_spsym / 8;
    for (int i = 0; i < n_ramp; ++i)
    {
        float env = (1 - cosf(2 * M_PI * i / (2 * n_ramp))) / 2;
        signal[i] *= env;
        signal[n_wave - 1 - i] *= env;
    }
}


int main(int argc, char **argv) {
    if (argc < 3 || argc > 4) return 2;
    float frequency = strtof(argv[2], NULL);
    int rate = argc == 4 ? atoi(argv[3]) : 48000;
    if (!(frequency >= 200 && frequency <= 2800) || (rate != 12000 && rate != 48000)) return 2;
    char to[14], de[14], extra[7], tail;
    if (sscanf(argv[1], "%13s %13s %6s %c", to, de, extra, &tail) != 3) return 2;
    ftx_message_t msg;
    if (ftx_message_encode_std(&msg, NULL, to, de, extra) != FTX_MESSAGE_RC_OK) return 3;
    uint8_t tones[79];
    ft8_encode(msg.payload, tones);
    int count = 79 * (int)(rate * .16f + .5f);
    float *wave = malloc(count * sizeof(float));
    int16_t *pcm = malloc(count * sizeof(int16_t));
    if (!wave || !pcm) return 1;
    synth_gfsk(tones, 79, frequency, 2.0f, .16f, rate, wave);
    for (int i=0; i<count; i++) pcm[i]=(int16_t)lrintf(wave[i]*6553);
    int ok = fwrite(pcm, sizeof(int16_t), count, stdout) == (size_t)count;
    free(wave); free(pcm); return ok ? 0 : 1;
}

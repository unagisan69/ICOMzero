APPBUILDER ?= ../AppBuilder

CC ?= cc
DECODER = build/icomzero-ft8-decode
FT8_SOURCES = $(wildcard vendor/ft8_lib/ft8/*.c)
FT8_SOURCES += vendor/ft8_lib/common/monitor.c
FT8_SOURCES += $(wildcard vendor/ft8_lib/fft/*.c)

.PHONY: package check test decoder
decoder: $(DECODER) build/icomzero-ft8-encode

$(DECODER): native/ft8_decode.c $(FT8_SOURCES) $(wildcard vendor/ft8_lib/ft8/*.h vendor/ft8_lib/fft/*.h vendor/ft8_lib/common/*.h)
	mkdir -p build
	$(CC) -O3 -std=gnu99 -Ivendor/ft8_lib native/ft8_decode.c $(FT8_SOURCES) -lm -o $@

build/icomzero-ft8-encode: native/ft8_encode.c $(FT8_SOURCES)
	mkdir -p build
	$(CC) -O3 -std=gnu99 -Ivendor/ft8_lib native/ft8_encode.c $(FT8_SOURCES) -lm -o $@

package: decoder
	umask 022; APPBUILDER="$$(cd "$(APPBUILDER)" && pwd)" && \
	bash "$$APPBUILDER/scripts/pack-deb.sh" "$(CURDIR)" --skip-build --output-dir "$(CURDIR)/build"

test: decoder
	PYTHONPATH=app python3 -m unittest discover -s tests -v

check:
	python3 -m py_compile app/icomzero app/*.py

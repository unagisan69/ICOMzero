APPBUILDER ?= ../AppBuilder

.PHONY: package check
package:
	umask 022; APPBUILDER="$$(cd "$(APPBUILDER)" && pwd)" && \
	bash "$$APPBUILDER/scripts/pack-deb.sh" "$(CURDIR)" --skip-build --output-dir "$(CURDIR)/build"

check:
	python3 -m py_compile app/icomzero app/radio.py

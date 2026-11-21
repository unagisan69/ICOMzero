APPBUILDER ?= ../AppBuilder

.PHONY: package check test
package:
	umask 022; APPBUILDER="$$(cd "$(APPBUILDER)" && pwd)" && \
	bash "$$APPBUILDER/scripts/pack-deb.sh" "$(CURDIR)" --skip-build --output-dir "$(CURDIR)/build"

test:
	PYTHONPATH=app python3 -m unittest discover -s tests -v

check:
	python3 -m py_compile app/icomzero app/radio.py app/civ.py app/models.py app/kenwood.py

.PHONY: install uninstall check archive

install:
	./install.sh

uninstall:
	./uninstall.sh

check:
	bash -n install.sh uninstall.sh bin/asus-custom-fan
	python3 -m py_compile src/fan_curve.py tests/test_curve.py
	python3 -m unittest -v tests.test_curve

archive:
	git archive --format=tar.gz -o asus-custom-fan-curve.tar.gz HEAD

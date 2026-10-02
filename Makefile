all:
	./build.py

full short:
	./build.py $@

watch:
	./build.py --watch

clean:
	./build.py clean

.PHONY: all full short watch clean

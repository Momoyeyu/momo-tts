.DEFAULT_GOAL := help

ENGINE ?= index
export ENGINE DATA_DIR DATA_ROOT GPU REFERENCE PROMPT LANGUAGE OUTPUT_LANGUAGE

.PHONY: help configure prepare bootstrap deploy restart rollback probe status set-reference install console
help:
	@printf '%s\n' 'make configure ENGINE=index|gpt DATA_DIR=/absolute/path GPU=GPU-uuid' \
		'make prepare|bootstrap|deploy|restart|rollback|probe|status ENGINE=index|gpt' \
		'make set-reference ENGINE=index|gpt REFERENCE=/path/voice.wav PROMPT="..." LANGUAGE=zh' \
		'make install [DATA_ROOT=/path/to/data]'

configure prepare bootstrap deploy restart rollback probe status set-reference install console:
	@python3 -m momo_tts.control $@

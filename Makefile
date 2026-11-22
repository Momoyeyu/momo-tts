.DEFAULT_GOAL := help

ENGINE ?=
export ENGINE DATA_ROOT ADMIN_BIND ADMIN_ORIGIN GPU REFERENCE PROMPT LANGUAGE OUTPUT_LANGUAGE

.PHONY: help configure prepare bootstrap deploy restart rollback probe status set-reference install console
help:
	@printf '%s\n' 'make configure ENGINE=index|gpt GPU=GPU-uuid' \
		'make prepare|bootstrap|deploy|restart|rollback|probe|status ENGINE=index|gpt' \
		'make set-reference ENGINE=index|gpt REFERENCE=/path/voice.wav PROMPT="..." LANGUAGE=zh' \
		'make deploy [DATA_ROOT=/path/to/data] [ADMIN_BIND=auto|loopback|private-ip]'

configure prepare bootstrap deploy restart rollback probe status set-reference install console:
	@python3 -m momo_tts.control $@

# momo IndexTTS 服务

momo 的 IndexTTS 2.5 推理服务。momo 设置可在 IndexTTS 和现有 GPT-SoVITS 之间切换，分别保存地址和密钥。两个引擎使用应用内同一份参考音频。GPT-SoVITS 的部署代码仍在 [momo/deploy/tts](https://github.com/Momoyeyu/momo/tree/master/deploy/tts)。

## 配置主机

需要 Linux x86_64、NVIDIA GPU、支持 CUDA 12.8 的驱动、用户级 systemd、Git、ffmpeg、`uv` 和可写的数据盘。安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后确保它在 PATH 中。选择有足够显存同时运行两个模型实例的 GPU，热重启时旧实例会等新实例完成实际合成后才退出。

```sh
nvidia-smi -L
findmnt -T /path/on/model-disk
./manage prepare /path/on/model-disk GPU-your-device-uuid
./manage bootstrap
./manage deploy /path/to/reference.wav
./manage probe --reference /path/to/reference.wav --language zh
```

在 ms33，本机机械阵列挂载于 `/data`。管理员可在该盘为运行服务的账号分配专用目录，再将实际路径传给 `prepare`。源代码、服务配置、API 密钥和轻量网关环境在用户目录；模型权重、Python/CUDA 环境及下载和推理缓存放在指定的数据盘。上游源码、锁文件和主权重快照均固定版本。辅助模型由官方下载器预先准备。下载失败后可运行 `./manage prepare-models` 续跑。模型运行时使用本地文件。

参考音频建议使用有权使用的 3–10 秒清晰语音。部署探针只使用指定的本地参考音，服务将请求中的参考音及合成中间文件置于私有临时目录，用完清理。代码库不提供参考音频，模型由 [IndexTTS](https://github.com/index-tts/index-tts) 与 [模型发布页](https://huggingface.co/IndexTeam/IndexTTS-2.5) 提供，使用前查阅其许可。

## 在 momo 中使用

只在主机回环地址监听。远程 GPU 建议通过 SSH 转发连接。保持终端中的隧道运行：

```sh
ssh -N -o ExitOnForwardFailure=yes -L 19880:127.0.0.1:19880 user@gpu-host
```

在 momo 打开「设置 → 模型 → 语音合成」，选择 IndexTTS，填入 `http://127.0.0.1:19880`，将服务器 `~/.config/momo-index/api-key` 的内容填入 API 密钥，再上传参考音频、点击播放图标。对话语言决定合成语言，目前 momo 可传中文、英文和日文。试听成功后可以在声音页控制语音音量和静音。

## 更新和热重启

```sh
git pull --ff-only
./manage deploy /path/to/reference.wav      # 使用当前 Git 提交
./manage restart /path/to/reference.wav     # 使用正在服务的提交
./manage rollback /path/to/reference.wav    # 切回另一槽最近保留的提交
./manage probe --reference /path/to/reference.wav --language en
journalctl --user -u 'momo-index-worker@*' -n 80
```

外部地址固定在 19880。更新时，空闲端口先加载模型并用参考音合成一段非静音 WAV。验证通过后才原子切换新请求，等旧请求结束再停止旧进程；候选合成失败会保留现有实例。首次启动前和主机重启后可用 `./manage probe` 确认就绪。若账号需要在退出 SSH 后或开机时自动运行服务，请确认该账号的 user systemd linger 已设好。

当前两个实例共享固定运行环境和权重。更新上游模型或 CUDA/Python 环境时需要单独维护窗口，常规热重启适用于推理服务代码和进程。

## HTTP 契约

- `GET /healthz`：网关存活。
- `GET /readyz`：当前模型是否就绪。
- `POST /v1/audio/speech`：JSON 参数为 `input`、`language`、`reference_audio`（base64）、`reference_name`，返回单声道 PCM16 WAV。参考文件上限 10 MiB。就绪及合成接口都要求 `Authorization: Bearer …`。

服务先合成完整 WAV，再由 momo 解码播放。接口检查可用 `python -m unittest discover -s tests -v`，无需 GPU。GPU 合成和切换需在部署主机另行运行探针验证。

# momo TTS

在自己的 GPU 服务器上运行 **IndexTTS** 和 **GPT-SoVITS**。用一段参考音生成语音，接入 [momo](https://momoyeyu.github.io/momo/) 或其他使用兼容接口的客户端。

- **任选引擎** · 两种引擎各有地址和 API 密钥，可以同时部署。
- **按需用显存** · momo 的预热请求会加载模型。客户端退出并且最后一条请求结束后，模型会释放显存。管理页和网关继续在线。
- **热更新与回滚** · 新版本先试合成，通过后才接新请求。旧请求可以播完，失败的候选版本会被撤下。
- **两种管理方式** · 用网页完成首次部署和日常管理，也可以全程用命令行。

## 快速开始

准备一台 **Linux x86_64 GPU 主机**。需要 NVIDIA 驱动支持 CUDA 12.8、可用的用户级 systemd、Python 3.11+、Git、Make、curl、ffmpeg 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。选择一个可写、空间充足的数据目录。热更新时，对应的 GPU 需要能同时容纳两个推理进程。

在 GPU 主机上执行：

```sh
git clone https://github.com/Momoyeyu/momo-tts.git
cd momo-tts
make deploy
```

`make deploy` 启动管理服务，默认把数据放在 `~/.local/share/momo-tts-data`。想用另一块盘，可以执行 `make deploy DATA_ROOT=/path/to/data`，也可以在首次配置引擎前从管理页指定。两种引擎会在公共目录下各自创建所需文件夹。

在 GPU 主机本机打开 `http://127.0.0.1:19890`。如果从另一台电脑连接，在自己的电脑上运行：

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 19890:127.0.0.1:19890 \
  -L 19880:127.0.0.1:19880 \
  -L 19884:127.0.0.1:19884 user@gpu-host
```

将 `user@gpu-host` 换成自己的 SSH 账号和主机。随后在浏览器打开同一地址。在 GPU 主机运行 `cat ~/.config/momo-tts/admin-key` 获取管理员密钥，粘贴到登录框。

如果服务器已有 HTTPS 反向代理，也可以用域名直接打开管理页。将代理的上游设为 `http://127.0.0.1:19890`，保留请求的 `Host`，然后运行 `make deploy ADMIN_ORIGIN=https://console.example.com`。管理页继续使用原有管理密钥。

## 在管理页完成第一次合成

1. 选一个引擎和它要用的 GPU。
2. 点击 **准备模型**，等待下载和依赖安装完成，再点击 **启动网关**。
3. 设置一段 3–10 秒、单人录制的单声道 PCM16 WAV 作为部署验证音。GPT-SoVITS 需要填选中的语言和录音原话。
4. 点击 **发布**，等待试合成通过。页面会显示已部署版本。空闲时模型显示为休眠。
5. 在 momo 打开「设置 → 模型 → 语音合成」，填入对应地址和 API 密钥。上传参考音，再点播放图标试听。

| 引擎 | 通过上述隧道访问 | 合成接口 |
| --- | --- | --- |
| IndexTTS 2.5 | `http://127.0.0.1:19880` | `POST /v1/audio/speech`，返回 WAV |
| GPT-SoVITS v2 | `http://127.0.0.1:19884` | `POST /tts`，支持流式输出 |

管理密钥与合成密钥独立。管理员可以从管理页复制相应引擎的 API 密钥。客户端使用 `Authorization: Bearer <API 密钥>` 访问合成及 `/readyz`。管理服务和推理网关默认只监听回环地址。

## 用命令行管理

```sh
make configure ENGINE=index GPU=GPU-your-device-uuid
make prepare ENGINE=index
make bootstrap ENGINE=index
make set-reference ENGINE=index REFERENCE=/path/to/reference.wav LANGUAGE=zh
make deploy ENGINE=index

make status ENGINE=index
make probe ENGINE=index OUTPUT_LANGUAGE=zh
make restart ENGINE=index
make rollback ENGINE=index
```

GPT-SoVITS 使用 `ENGINE=gpt`，在 `set-reference` 时附上 `PROMPT='录音原话'`。GPU UUID 可用 `nvidia-smi -L` 查询。两个引擎的合成密钥文件分别为 `~/.config/momo-index/api-key` 和 `~/.config/momo-gpt/api-key`。

部署参考音只用于探针和发布验证。momo 在实际合成时使用客户端里上传的参考音。选择你有权使用的录音，并查阅两个上游项目的模型许可。[IndexTTS](https://github.com/index-tts/index-tts) · [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)

### 日常运维

`make deploy ENGINE=index` 发布引擎的当前 Git 提交，`restart` 用当前线上版本热重启，`rollback` 切回上一版。更新共享模型或 CUDA/Python 环境时，给对应引擎留出维护窗口。模型下载失败后可重跑 `make prepare`。需要在退出 SSH 或主机重启后保留服务时，请为运行账号启用用户级 systemd linger。

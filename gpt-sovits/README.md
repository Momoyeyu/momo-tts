# GPT-SoVITS

固定 GPT-SoVITS v2 的推理服务。原生 Linux 发布使用 Python 3.11 和 PyTorch 2.8 CUDA 12.8。模型、依赖和推理缓存都放在配置的数据盘，推理时使用本地模型。已安装旧版 Docker 的环境仍可按原编排启动。

在仓库根目录用 `make` 准备模型、发布、热重启和回滚。部署说明见 [仓库文档](../README.md) 和 [语音使用与部署](https://momoyeyu.github.io/momo/help/voice.html)。

通过 momo 使用时，需要填写服务地址和 API 密钥。中文台词建议用清晰的中文参考音及它的逐字台词。语音和台词对应时更方便判断语速、咬字和音色是否适合角色。

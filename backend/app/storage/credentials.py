# API Key 单独放在操作系统凭据库；SQLite 仅保存配置对应的引用 ID。
# 凭据服务不可用时明确失败，不回退到 JSON、环境文件或数据库明文保存。
import keyring


class Credentials:
    # 固定服务名称使不同应用发布版本能继续读取用户原来保存的密钥。
    service = "GALAgent.ModelConnections"

    def put(self, ref, secret):
        try:
            keyring.set_password(self.service, ref, secret)
        except Exception:
            raise ValueError("系统凭据存储不可用，密钥未保存。请检查 Windows 凭据管理器。") from None

    def get(self, ref):
        # 读取失败与“没有该密钥”都返回面向用户的错误，不暴露底层异常内容。
        try:
            value = keyring.get_password(self.service, ref)
        except Exception:
            raise ValueError("无法读取系统保存的 API Key") from None
        if not value:
            raise ValueError("此模型配置尚未保存 API Key，请重新填写")
        return value

    def delete(self, ref):
        # 已不存在的密钥视为删除完成；其他凭据库错误仍需通知用户。
        try:
            keyring.delete_password(self.service, ref)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception:
            raise ValueError("无法删除系统保存的 API Key") from None

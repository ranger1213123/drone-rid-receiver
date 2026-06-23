"""
mTLS 证书管理 — CA 自签 + 设备客户端证书签发/吊销

证书用途:
  - CA 根证书 → EMQX ssl_options.cacertfile (验证客户端)
  - 设备证书 → MQTT CONNECT 时作为客户端证书
  - 服务端证书 → MQTT Consumer 连接 EMQX 时使用

存储: CA 私钥仅存在于 K8s Secret; DeviceSecret 表存储证书 PEM
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

logger = logging.getLogger(__name__)

CA_VALIDITY_YEARS = 20
DEVICE_CERT_VALIDITY_YEARS = 10


def _generate_ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _serialize_private_key(key) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _serialize_cert(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")


class CertManager:
    """CA 证书管理 + 设备证书生命周期"""

    def __init__(self, ca_cert_pem: str = None, ca_key_pem: str = None):
        if ca_cert_pem and ca_key_pem:
            self._ca_key = serialization.load_pem_private_key(
                ca_key_pem.encode("utf-8"), password=None,
            )
            self._ca_cert = x509.load_pem_x509_certificate(
                ca_cert_pem.encode("utf-8"),
            )
            logger.info("CA 证书已从环境变量加载")
        else:
            logger.info("CA 证书未提供，将在首次 issue_device_cert 时自签生成")
            self._ca_key = None
            self._ca_cert = None

    @property
    def ca_cert_pem(self) -> Optional[str]:
        return _serialize_cert(self._ca_cert) if self._ca_cert else None

    @property
    def ca_key_pem(self) -> Optional[str]:
        return _serialize_private_key(self._ca_key) if self._ca_key else None

    @property
    def initialized(self) -> bool:
        return self._ca_cert is not None

    def ensure_ca(self):
        """确保 CA 已初始化，否则生成自签根证书"""
        if self._ca_cert is not None:
            return

        logger.info("生成自签 CA 根证书 (有效期 %d 年)", CA_VALIDITY_YEARS)
        self._ca_key = _generate_ec_key()

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Drone RID System"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Drone RID Internal CA"),
        ])

        now = datetime.now(timezone.utc)
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=CA_VALIDITY_YEARS * 365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                key_cert_sign=True, crl_sign=True,
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                self._ca_key.public_key(),
            ), critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )
        logger.info("CA 根证书已生成, CN=%s", "Drone RID Internal CA")

    def issue_device_cert(self, device_name: str) -> dict:
        """为设备签发客户端 X.509 证书

        Returns: {cert, key, ca_cert, serial}
        """
        self.ensure_ca()

        device_key = _generate_ec_key()
        now = datetime.now(timezone.utc)
        serial = x509.random_serial_number()

        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Drone RID Edge"),
            x509.NameAttribute(NameOID.COMMON_NAME, device_name),
        ])

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(device_key.public_key())
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=DEVICE_CERT_VALIDITY_YEARS * 365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=True,
                key_encipherment=True, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
                key_cert_sign=False, crl_sign=False,
            ), critical=True)
            .add_extension(x509.ExtendedKeyUsage([
                x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
            ]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(
                device_key.public_key(),
            ), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                self._ca_key.public_key(),
            ), critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )

        cert_serial_hex = format(serial, 'x')
        logger.info("设备证书已签发: device=%s serial=%s", device_name, cert_serial_hex)

        return {
            "cert": _serialize_cert(cert),
            "key": _serialize_private_key(device_key),
            "ca_cert": _serialize_cert(self._ca_cert),
            "serial": cert_serial_hex,
        }

    def revoke_device_cert(self, device_name: str) -> bool:
        """吊销设备证书 — 标记 revoked 并清除 client_cert"""
        from .models import get_session, DeviceSecret
        sess = get_session()
        d = sess.get(DeviceSecret, device_name)
        if not d:
            logger.warning("设备不存在, 无法吊销: %s", device_name)
            return False
        d.revoked = True
        d.revoked_at = datetime.now(timezone.utc)
        d.client_cert = None  # 清除已泄露的证书
        sess.commit()
        logger.warning("设备证书已吊销: device=%s serial=%s", device_name, d.cert_serial or "N/A")
        return True

    def is_device_revoked(self, device_name: str) -> bool:
        """检查设备证书是否已吊销"""
        from .models import get_session, DeviceSecret
        sess = get_session()
        d = sess.get(DeviceSecret, device_name)
        return d is not None and d.revoked

    def get_device_cert_info(self, device_name: str) -> Optional[dict]:
        """查询已签发证书信息 (从 DeviceSecret 表)"""
        from .models import get_session, DeviceSecret
        sess = get_session()
        d = sess.get(DeviceSecret, device_name)
        if d and d.client_cert:
            return {
                "device_name": d.device_name,
                "cert_serial": d.cert_serial,
                "cert_issued_at": d.cert_issued_at.isoformat() if d.cert_issued_at else "",
            }
        return None


# 全局单例 (由 Flask app 初始化)
_cert_manager: Optional[CertManager] = None


def get_cert_manager() -> CertManager:
    global _cert_manager
    if _cert_manager is None:
        ca_cert = os.environ.get("CA_CERT", "")
        ca_key = os.environ.get("CA_KEY", "")
        _cert_manager = CertManager(
            ca_cert_pem=ca_cert if ca_cert else None,
            ca_key_pem=ca_key if ca_key else None,
        )
    return _cert_manager

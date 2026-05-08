from pyinfra.operations import apt, files, systemd

from cmdeploy.basedeploy import Deployer, get_resource


class Fail2BanDeployer(Deployer):
    def install(self):
        apt.packages(
            name="Install Fail2Ban",
            packages=["fail2ban"],
        )

    def configure(self):
        filter_config = files.put(
            name="Install Dovecot TLS scan Fail2Ban filter",
            src=get_resource("fail2ban/filter.d/chatmail-dovecot-tls-scan.conf"),
            dest="/etc/fail2ban/filter.d/chatmail-dovecot-tls-scan.conf",
            user="root",
            group="root",
            mode="644",
        )
        jail_config = files.put(
            name="Install Dovecot TLS scan Fail2Ban jail",
            src=get_resource("fail2ban/jail.d/chatmail-dovecot-tls-scan.conf"),
            dest="/etc/fail2ban/jail.d/chatmail-dovecot-tls-scan.conf",
            user="root",
            group="root",
            mode="644",
        )
        self.need_restart = filter_config.changed or jail_config.changed

    def activate(self):
        systemd.service(
            name="Start and enable Fail2Ban",
            service="fail2ban.service",
            running=True,
            enabled=True,
            restarted=self.need_restart,
        )
        self.need_restart = False

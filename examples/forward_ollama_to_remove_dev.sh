ssh -N -R 0.0.0.0:11434:127.0.0.1:11434 user@remote_dev_machine -o ExitOnForwardFailure=yes

# set remote server local gateway port config
echo 'GatewayPorts clientspecified' | sudo tee /etc/ssh/sshd_config.d/99-gatewayports.conf

# reset sshd
sudo systemctl reload ssh || sudo systemctl reload sshd
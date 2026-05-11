from cmdeploy.basedeploy import get_resource


def test_postfix_smtp_client_prefers_ipv4():
    template = get_resource("postfix/main.cf.j2").read_text()

    assert "smtp_address_preference = ipv4" in template

"""
One-time "set your password" link for a freshly provisioned dashboard login.

Why not just email a temporary password: it sat in plaintext in the welcome
email, forever valid until the customer bothered to change it, in a mailbox we
don't control. A link is single-use by construction — the token embeds the
password hash, so the moment the customer sets their own it stops working —
and it carries nothing that grants access on its own once used.

Why not Django's stock reset token: it expires in PASSWORD_RESET_TIMEOUT
(1 hour), which is right for "I forgot my password" and wrong for "here is
your account", which people open the next morning. This generator keeps
Django's construction and signing and only changes the salt (so a welcome
token can never be replayed as a reset token, or vice versa) and the window.
"""
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int


class WelcomeTokenGenerator(PasswordResetTokenGenerator):
    key_salt = 'landing.tokens.WelcomeTokenGenerator'
    timeout = 7 * 24 * 3600      # seconds; the welcome email is opened days later

    def check_token(self, user, token):
        # Django 5.2's check_token verbatim, except the timeout comes from the
        # class instead of settings.PASSWORD_RESET_TIMEOUT.
        if not (user and token):
            return False
        try:
            ts_b36, _ = token.split('-')
        except ValueError:
            return False
        try:
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False
        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(self._make_token_with_timestamp(user, ts, secret), token):
                break
        else:
            return False
        if (self._num_seconds(self._now()) - ts) > self.timeout:
            return False
        return True


welcome_token = WelcomeTokenGenerator()

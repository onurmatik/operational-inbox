from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inbox', '0004_signupattempt'),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='signupattempt',
            name='inbox_signu_fingerp_c51b87_idx',
        ),
        migrations.AddField(
            model_name='signupattempt',
            name='kind',
            field=models.CharField(choices=[('SIGNUP', 'Signup'), ('VERIFICATION_RESEND', 'Verification resend')], default='SIGNUP', max_length=24),
        ),
        migrations.AddIndex(
            model_name='signupattempt',
            index=models.Index(fields=['kind', 'fingerprint_hash', '-created_at'], name='inbox_signu_kind_936a90_idx'),
        ),
        migrations.AddIndex(
            model_name='signupattempt',
            index=models.Index(fields=['kind', 'email_hash', '-created_at'], name='inbox_signu_kind_92352b_idx'),
        ),
    ]

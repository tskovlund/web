# Generated by Django 2.2.4 on 2019-08-11 19:04

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("games", "0004_remove_user_old_password_hash")]

    operations = [
        migrations.AlterField(
            model_name="playerstat",
            name="total_chugs",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="playerstat",
            name="total_games",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="playerstat",
            name="total_sips",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="playerstat",
            name="total_time_played_seconds",
            field=models.FloatField(default=0),
        ),
    ]

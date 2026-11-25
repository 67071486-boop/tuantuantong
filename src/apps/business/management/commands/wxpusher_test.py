import os

from django.core.management.base import BaseCommand, CommandError

from apps.core.wxpusher import WxPusherClient, WxPusherError


class Command(BaseCommand):
    help = "用 WXPUSHER_APP_TOKEN 向已订阅测试应用的指定 UID 发送一条测试提醒。"

    def add_arguments(self, parser):
        parser.add_argument("--uid", required=True, help="测试收件人自己的 WxPusher UID")
        parser.add_argument("--url", help="可选的完整 HTTPS 回跳链接")

    def handle(self, *args, **options):
        app_token = os.environ.get("WXPUSHER_APP_TOKEN", "").strip()
        uid = options["uid"].strip()
        if not app_token:
            raise CommandError("请先在环境变量 WXPUSHER_APP_TOKEN 中设置测试应用密钥")
        if not uid:
            raise CommandError("请用 --uid 指定已订阅测试应用的收件人")
        try:
            with WxPusherClient() as client:
                content_id = client.send(
                    app_token=app_token, uid=uid, content="这是一条团团通内部沟通测试提醒。",
                    summary="团团通测试提醒", url=options["url"],
                )
        except WxPusherError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(f"WxPusher 已受理消息 {content_id}，请在测试设备确认送达。")

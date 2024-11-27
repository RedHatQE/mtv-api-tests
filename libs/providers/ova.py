from libs.base_provider import BaseProvider


class OVAProvider(BaseProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def disconnect(self):
        return True

    def connect(self):
        return True

    @property
    def test(self):
        return True

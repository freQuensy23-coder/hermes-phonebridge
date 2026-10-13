class BridgeError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self):
        return {"error": {"code": self.code, "message": self.message, **self.details}}

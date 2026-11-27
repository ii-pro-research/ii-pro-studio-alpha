class DockerApprovalRequired(RuntimeError):
    pass


class DockerUnavailable(RuntimeError):
    pass


class DockerConfigMismatch(RuntimeError):
    pass


class DockerStartUncertain(RuntimeError):
    def __init__(self, identity, process=None):
        super().__init__("Docker start may have been dispatched")
        self.identity, self.process = identity, process

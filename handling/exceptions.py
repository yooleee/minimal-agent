class GeneralException(RuntimeError):

    """ Raised to interrupt the agent flow and add messages"""

    def __init__(self, *messages: dict):
        self.messages = messages
        super().__init__()

class TerminatingException(GeneralException):

    " Raised when the session is terminated"

class NonTerminatingException(GeneralException):

    """ Raised to interrupt the agent flow and add messages"""

class FormatError(NonTerminatingException):

    " Raised when formatting is invalid, inform the model of the required format"

class Submitted(TerminatingException):

    """ Raised when task is complete and the session is terminating."""

class ExecutionTimeout(NonTerminatingException):

    """ Raised when a command timeouts, the model should attempt a different direction."""

class StepLimit(TerminatingException):

    """ Raised when the # of steps is over the limit terminating the session. """

# List the files in the current directory. Run the exit command once complete.

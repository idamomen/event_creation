import json
import os
import copy
from transferer import DB_ROOT, UnTransferrableException
from pipelines import build_events_pipeline, build_split_pipeline, build_convert_events_pipeline, \
                      build_convert_eeg_pipeline, build_import_montage_pipeline
from ptsa.data.readers.IndexReader import JsonIndexReader

class Importer(object):

    MONTAGE = 1
    BUILD_EVENTS = 2
    BUILD_EPHYS = 3
    CONVERT_EVENTS = 4
    CONVERT_EPHYS = 5

    PIPELINE_BUILDERS = {
        MONTAGE: build_import_montage_pipeline,
        BUILD_EVENTS: build_events_pipeline,
        BUILD_EPHYS: build_split_pipeline,
        CONVERT_EVENTS: build_convert_events_pipeline,
        CONVERT_EPHYS: build_convert_eeg_pipeline
    }
    LABELS = {
        MONTAGE: 'Montage Importer',
        BUILD_EVENTS: 'Events Builder',
        BUILD_EPHYS: 'Ephys Builder',
        CONVERT_EVENTS: 'Events Converter',
        CONVERT_EPHYS: 'Ephys Converter'
    }

    def __init__(self, type, *args, **kwargs):
        if type not in self.PIPELINE_BUILDERS:
            raise UnTransferrableException("Cannot build importer for type {}".format(type))
        self.label = self.LABELS[type]
        self.args = args
        self.kwargs = kwargs
        self.subject = kwargs['subject']
        self.errors = {'init': None, 'check': None, 'transfer': None, 'processing': None}
        self._should_transfer = None
        self.errored = False
        self.processed = False
        self.transferred = False
        try:
            self.pipeline = self.PIPELINE_BUILDERS[type](*args, **kwargs)
            self.transferer = self.pipeline.transferer
            self.initialized = True
        except Exception as e:
            self.set_error('init', e)
            self.pipeline = None
            self.transferer = None
            self.initialized = False

    def describe(self):
        label = self.label
        label += ':: ' + ', '.join([k + ': ' + str(v) for k, v in self.kwargs.items()])
        statuses = [label]
        initialization_status = 'Initialization status: '
        if self.initialized:
            initialization_status += "success"
        else:
            initialization_status += "failure"
        statuses.append(initialization_status)

        transfer_status = 'Transfer status: '
        if self.should_transfer:
            if self.errors['transfer']:
                transfer_status += 'necessary, failed'
            elif not self.transferred:
                transfer_status += 'necessary, incomplete'
            else:
                transfer_status += 'complete'
        elif self.errors['check']:
            transfer_status += 'failed to compute checksum'
        else:
            transfer_status += 'not necessary'
        statuses.append(transfer_status)

        if self.transferred:
            processed_status = 'Processed status: '
            if self.errors['processing']:
                processed_status += 'necessary, failed'
            elif not self.processed:
                processed_status += 'necessary, incomplete'
            else:
                processed_status += 'complete'
            statuses.append(processed_status)

        if self.errored:
            error_status = self.describe_errors()
            statuses.append(error_status)

        return '\n'.join(statuses)

    def describe_errors(self):
        errors = []
        if self.errors['init']:
            errors.append('Initialization error: {}'.format(self.errors['init']))
        if self.errors['check']:
            errors.append('Checksum calculation error: {}'.format(self.errors['check']))
        if self.errors['transfer']:
            errors.append('Transfer error: {}'.format(self.errors['transfer']))
        if self.errors['processing']:
            errors.append('Processing error: {}'.format(self.errors['processing']))
        return '\n'.join(errors)

    def set_error(self, error_type, error):
        self.errors[error_type] = error
        self.errored = True

    @property
    def should_transfer(self):
        if self.initialized and self._should_transfer is None:
            self.check()
        return self._should_transfer

    def check(self):
        try:
            self._should_transfer = self.transferer.check_checksums()
        except Exception as e:
            self.set_error('check', e)
            self._should_transfer = False
        return self._should_transfer

    def run(self):
        try:
            self.pipeline.run()
            self.processed = True
            self.transferred = True
        except UnTransferrableException as e:
            self.set_error('transfer', e)
        except Exception as e:
            self.set_error('processing', e)

class Automator(object):

    EXPERIMENTS = {'r1': ('FR1', 'FR2', 'FR3',
                          'catFR1', 'catFR2', 'catFR3',
                          'PAL1', 'PAL2', 'PAL3',
                          'TH1', 'TH2', 'TH3',
                          'PS1', 'PS2', 'PS3')}
    MATH_TASKS = ('FR1', 'FR2', 'FR3', 'catFR1', 'catFR2', 'catFR3', 'PAL1', 'PAL2', 'PAL3', 'ltpFR', 'ltpFR2')

    INCLUDE_TRANSFERRED = False

    def __init__(self, protocol):
        self.protocol = protocol
        self.index = JsonIndexReader(os.path.join(DB_ROOT, 'protocols', '{}.json'.format(protocol)))
        self.importers = []

    def populate_importers(self):
        self.add_existing_events_importers()
        self.add_existing_montage_importers()
        self.add_future_events_importers()

    def add_existing_montage_importers(self):
        subjects = self.index.subjects()
        for subject in subjects:
            montages = self.index.montages(subject=subject)
            for montage in montages:
                code = self.index.get_value('subject_alias', subject=subject, montage=montage.split('.')[1])
                importer = Importer(Importer.MONTAGE,
                                    subject=subject, montage=montage, protocol=self.protocol, code=code)
                if importer.check() or importer.errored or self.INCLUDE_TRANSFERRED:
                    self.importers.append(importer)

    def session_indexes(self):
        experiments = self.index.experiments()
        for experiment in experiments:
            exp_index = self.index.filtered(experiment=experiment)
            subjects = exp_index.subjects()
            for subject in subjects:
                subj_index = exp_index.filtered(subject=subject)
                sessions = subj_index.sessions()
                for session in sessions:
                    sess_index = subj_index.filtered(session=session)
                    yield subject, experiment, session, sess_index

    def build_existing_event_importer_kwargs(self, subject, experiment, session, index, do_compare=True):
        montage = index.montages()[0]
        do_math = experiment in self.MATH_TASKS
        code = index.get_value('subject_alias')
        try:
            original_session = int(index.get_value('original_session'))
        except KeyError:
            original_session = int(session)
        try:
            original_experiment = index.get_value('original_experiment')
        except KeyError:
            original_experiment = experiment

        kwargs = dict(subject=subject, montage=montage, experiment=original_experiment, session=int(session),
                      new_experiment=experiment, original_session=original_session,
                      do_math=do_math, protocol=self.protocol, code=code, do_compare=do_compare)
        return kwargs


    def add_existing_events_importers(self):
        for subject, experiment, session, index in self.session_indexes():
            kwargs = self.build_existing_event_importer_kwargs(subject, experiment, session, index)
            importer = Importer(Importer.BUILD_EVENTS, **kwargs)
            if not importer.check() and not importer.errored:
                pass
            elif not importer.errored:
                self.importers.append(importer)
            else:
                importer2 = Importer(Importer.CONVERT_EVENTS, **kwargs)
                if not importer2.check() and not importer2.errored:
                    pass
                else:
                    if not importer.errored:
                        self.importers.append(importer)
                    elif not importer2.errored:
                        self.importers.append(importer2)
                    else:
                        self.importers.append(importer)

    def add_future_events_importers(self):
        subjects = self.index.subjects()
        for subject in subjects:
            subj_index = self.index.filtered(subject=subject)
            montages = subj_index.montages()
            max_montage = max(montages)
            for experiment in self.EXPERIMENTS:
                exp_index = subj_index.filtered(montage=max_montage, experiment=experiment)
                sessions = exp_index.sessions()
                if sessions:
                    max_session = max(sessions)
                    try:
                        original_session = exp_index.get_value('original_session', session=max_session) + 1
                    except KeyError:
                        original_session = max_session+1
                    try:
                        original_experiment = exp_index.get_value('original_experiment', session=max_session)
                    except KeyError:
                        original_experiment = experiment
                    session = max_session + 1
                else:
                    session = 0
                    original_session = 0
                    original_experiment = experiment
                do_math = experiment in self.MATH_TASKS
                code = subj_index.get_value('subject_alias', montage=max_montage.split('.')[1])
                kwargs = dict(subject=subject, montage=max_montage, experiment=original_experiment, session=session,
                              new_experiment=experiment, original_session=original_session,
                              do_math=do_math, protocol=self.protocol, code=code, do_compare=False)
                importer = Importer(Importer.BUILD_EVENTS, **kwargs)
                if importer.check():
                    self.importers.append(importer)

    def run_all_imports(self):
        for importer in self.importers:
            importer.run()

    def sorted_importers(self):
        order = 'initialized', 'errored', '_should_transfer', 'transferred', 'processed', 'subject'
        return sorted(self.importers, key=lambda imp: [imp.__dict__[o] for o in order])

    def describe(self):
        descriptions = []
        if not self.importers:
            return 'No Importers'
        for importer in self.sorted_importers():
            descriptions.append(importer.describe())
        return '\n---------------\n'.join(descriptions)

def xtest_future_events():
    automator = Automator('r1')
    automator.add_future_events_importers()
    print automator.describe()

def test_get_altered_montages():
    automator = Automator('r1')
    automator.populate_importers()
    print automator.describe()

if __name__ == '__main__':
    automator = Automator('r1')
    automator.populate_importers()
    automator.run_all_imports()
    print automator.describe()
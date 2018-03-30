import os
import mne
import nolds
import numpy as np
import scipy.stats as ss
from ..log import logger


class ArtifactDetector:
    """
    Runs scripts for Scalp Lab blink and artifact detection. Parameters differ depending on whether the session was
    conducted using EGI or Biosemi. After detection processes are run, the events structure is filled with artifact data.
    """

    def __init__(self, events, eeg, ephys_dir):
        """
        :param events: The events structure (a recarray) for the session
        :param eeg: A dictionary matching the basename of each EEG recording to its data (designed for cases with 
        multiple recordings from a single session).
        :param ephys_dir: The path to the current_processed ephys folder for the current session.
        """
        self.events = events
        self.eegfile = None  # Used for tracking the path to the recording which is currently being processed
        self.eeg = eeg
        self.ephys_dir = ephys_dir

        self.system = None
        self.chans = None
        self.n_chans = None
        self.eog_chans = None
        self.bad_chans = None
        self.blink_thresh = None

    def run(self):
        """
        Adds artifact testing information to the events structure and records bad channels and bad-channel testing
        scores in a TSV file for each EEG recording in the session.

        :return: The events structure updated with artifact and blink information.
        """
        if self.events.shape == () or len(self.eeg) == 0 or np.sum(self.events.type == 'WORD') == 0:
            logger.warn('Skipping artifact detection due to there being no word presentation events or no EEG data!')
        else:
            for self.eegfile in self.eeg:
                # Drop miscellaneous/sync pulse channel(s)
                self.eeg[self.eegfile].pick_types(eeg=True, eog=True)

                # Prepare settings depending on the EEG system that was used
                if self.eegfile.endswith('.bdf'):
                    self.system = 'bio'
                    self.left_eog = ['EXG3', 'EXG1']
                    self.right_eog = ['EXG4', 'EXG2']
                elif self.eegfile.endswith('.mff') or self.eegfile.endswith('.raw'):
                    self.system = 'egi'
                    self.left_eog = ['E25', 'E127']
                    self.right_eog = ['E8', 'E126']
                else:
                    logger.warn('Unidentifiable EEG system detected in file %s' % self.eegfile)
                    continue

                # Set bipolar reference for EOG channels. Note that the resulting channels will be anode - cathode
                self.eeg[self.eegfile] = mne.set_bipolar_reference(self.eeg[self.eegfile], anode=[self.left_eog[0],
                                                    self.right_eog[0]], cathode=[self.left_eog[1], self.right_eog[1]])

                # Get a list of the channels names, and make sure we have 130 channels as intended (128 +
                self.chans = np.array(self.eeg[self.eegfile].ch_names)
                self.n_chans = len(self.chans)
                if (self.eegfile.endswith('.mff') or self.eegfile.endswith('.raw')) and self.n_chans != 126:
                    logger.warn('Artifact detection expected 124 EEG + 2 bipolar EOG channels for EGI but got %i for '
                                'file %s! Skipping...' % (self.n_chans, self.eegfile))
                    continue
                elif self.eegfile.endswith('.bdf') and self.n_chans != 130:
                    logger.warn(
                        'Artifact detection expected 128 EEG + 2 bipolar EOG channels for BioSemi but got %i for '
                        'file %s! Skipping...' % (self.n_chans, self.eegfile))
                    continue

                # Record the indices of the bipolar EOG channels, as positioned in the mne object
                self.leog_ind = self.eeg[self.eegfile].ch_names.index(self.left_eog[0] + '-' + self.left_eog[1])
                self.reog_ind = self.eeg[self.eegfile].ch_names.index(self.right_eog[0] + '-' + self.right_eog[1])

                # Create mask to select only EEG channels (not EOG)
                self.eeg_mask = np.ones(self.n_chans, dtype=bool)
                self.eeg_mask[self.reog_ind] = False
                self.eeg_mask[self.leog_ind] = False

                # Run artifact detection
                self.mark_bad_channels()
                self.mark_bad_epochs()

        return self.events


    def mark_bad_channels(self):
        """
        Runs several bad channel detection tests, records the test scores in a TSV file, and saves the list of bad
        channels to a text file. The detection methods are as follows:

        1) High voltage offset from the reference channel. This corresponds to the electrode offset screen in BioSemi's
        ActiView, and can be used to identify channels with poor connection to the scalp. The percent of the recording
        during which the voltage offset exceeds 30 mV is calculated for each channel. Any channel that spends more than
        15% of the total duration of the recording above this offset threshold is marked as bad.

        2) Log-transformed variance of the channel. The variance is useful for identifying both flat channels and
        extremely noisy channels. Because variance has a log-normal distribution across channels, log-transforming the
        variance allows for more reliable detection of outliers.

        3) Hurst exponent of the channel. The Hurst exponent is a measure of the long-range dependency of a time series.
        As physiological signals consistently have a Hurst exponent of around .7, channels with extreme deviations from
        this value are unlikely to be measuring physiological activity.

        Note that high-pass filtering is required prior to calculating the variance and Hurst exponent of each channel,
        as baseline drift will artificially increase the variance and invalidate the Hurst exponent.

        Through parameter optimization, it was found that channels should be marked as bad if they have a z-scored Hurst
        exponent greater than 3.1 or z-scored log variance less than -1.9 or greater than 1.7. This combination of
        thresholds, alongside the voltage offset test, successfully identified ~80.5% of bad channels with a false
        positive rate of ~2.9% when tested on a set of 20 manually-annotated sessions. It was additionally found that
        marking bad channels based on a low Hurst exponent failed to identify any channels that had not already marked
        by the log-transformed variance test. Similarly, marking channels that were poorly correlated with other
        channels as bad (see the "FASTER" method by Nolan, Whelan, and Reilly (2010)) was an accurate metric, but did
        not improve the hit rate beyond what the log-transformed variance and Hurst exponent could achieve on their own.

        Optimization was performed using a simple grid search of z-score threshold combinations for the different bad
        channel detection methods, with the goal of optimizing the trade-off between hit rate and false positive rate
        (hit_rate - false_positive_rate). The false positive rate was weighted at either 5 or 10 times the hit rate, to
        strongly penalize the system for throwing out good channels (both weightings produced similar optimal
        thresholds).

        Following bad channel detection, two bad channel files are created. The first is a file named
        <eegfile_basename>_bad_chan.txt, and is a text file containing the names of all channels that were identifed
        as bad. The second is a tab-separated values (.tsv) file called <eegfile_basename>_bad_chan_info.tsv, which
        contains the actual detection scores for each EEG channel.

        :return: None
        """
        logger.debug('Identifying bad channels for %s' % self.eegfile)

        # Set thresholds for bad channel criteria (see docstring for details on how these were optimized)
        offset_th = .03  # Samples over ~30 mV (.03 V) indicate poor contact with the scalp (BioSemi only)
        offset_rate_th = .15  # If >15% of the recording has poor scalp contact, mark as bad (BioSemi only)
        low_var_th = -1.9  # If z-scored log variance < 1.9, channel is most likely flat
        high_var_th = 1.7  # If z-scored log variance > 1.7, channel is likely too noisy to analyze
        hurst_th = 3.1  # If z-scored Hurst exponent > 3.1, channel is unlikely to be physiological

        # Select EEG channels (not EOG or other channels) from just the currently active EEG file
        eeg = self.eeg[self.eegfile].copy()
        eeg.pick_types(eeg=True, eog=False)

        # Method 1: Percent of samples with a high voltage offset (>30 mV) from the reference channel
        if self.system == 'bio':
            ref_offset = np.mean(np.abs(eeg._data) > offset_th, axis=1)
        else:
            ref_offset = np.zeros(self.n_chans)

        # Apply .5 Hz high pass filter to prevent baseline drift from affecting the variance and Hurst exponent
        eeg.filter(.5, None, fir_design='firwin')

        # Method 2: High or low log-transformed variance
        var = np.log(np.var(eeg._data, axis=1))
        zvar = ss.zscore(var)

        # Method 3: High Hurst exponent
        hurst = np.zeros(self.n_chans)
        for i in range(len(hurst)):
            hurst[i] = nolds.hurst_rs(eeg._data[i, :])
        zhurst = ss.zscore(hurst)

        # Identify bad channels using optimized thresholds
        bad = np.where((ref_offset > offset_rate_th) | (zvar < low_var_th) | (zvar > high_var_th) | (zhurst > hurst_th))
        badch = self.chans[bad]

        # Save list of bad channels to a text file
        badchan_file = os.path.join(self.ephys_dir, os.path.splitext(os.path.basename(self.eegfile))[0] + '_bad_chan.txt')
        np.savetxt(badchan_file, badch, fmt='%s')

        # Save a TSV file with extended info about each channel's scores
        badchan_file = os.path.join(self.ephys_dir, os.path.splitext(os.path.basename(self.eegfile))[0] + '_bad_chan_info.tsv')
        with open(badchan_file, 'w') as f:
            f.write('name\thigh_offset_rate\tlog_var\thurst\tbad\n')
            for i, ch in enumerate(self.chans):
                    f.write('%s\t%f\t%f\t%f\t%i\n' % (ch, ref_offset[i], var[i], hurst[i], bad[i]))

    def mark_bad_epochs(self):
        """
        Runs several bad epoch detection tests -- some on individual channels and others across channels. The test
        scores and automatically marked bad epochs/bad channels on each epoch are marked in the events structure. The
        detection methods for cross-channel bad epochs are as follows:

        1) Variance during the event, calculated for each channel then averaged across channels. This method is
        designed to identify events which have large amounts of noise across many or all channels.

        2) Amplitude range during the event, calculated for each channel then averaged across channels. This method is
        designed to identify events contaminated with high-amplitude artifacts or large baseline shifts across many or
        all channels.

        Entire presentation events are automatically marked as bad if the average variance across channels (z-scored
        across events) is greater than 3 or if the average amplitude range across channels (z-scored across events) is
        greater than 3. Note that both of these methods are adapted from the "FASTER" method by Nolan, Whelan, and
        Reilly (2010).

        The detection methods for marking individual channels as bad during each event are as follows:

        1) Variance of the channel during the event. Extremely high variance indicates that the channel was noisy during
        the event.

        2) Median slope of the channel during the event. A high median slope is characteristic of high-frequency
        artifacts that often originate from muscle activity.

        3) Amplitude range of the channel during the event. A high amplitude range may be indicative of a high-amplitude
        artifact or a large shift in baseline.

        4) Max/min deviation of the voltage from the channel's interquartile range. The interquartile range is
        calculated for each channel across all time points and presentation events. Deviation is measured as the maximum
        number of interquartile ranges above the 75th percentile or below the 25th percentile that the channel's voltage
        reaches during an event. This is an effective method for identifying high-amplitude artifacts, including blinks.

        Individual channels on each event are marked as bad if the variance of the channel (z-scored across events) is
        greater than 3, if the median slope of the channel during the event (z-scored across events) is greater than 3,
        if the amplitude range of the channel (z-scored across events) is greater than 3, and if the channel's voltage
        reaches 3 IQRs above its 75th percentile/below its 25th percentile at any point during the event. Methods 1, 2,
        and 3 were based on the methods used in "FASTER".

        Blink/eye movement detection is performed by applying method 4 to each EOG channel. Events where an EOG channel
        exceeds the 3*IQR threshold are marked as having an EOG artifact.

        The scores and automatic artifact markings for each word presentation are saved into the events structure
        directly.

        Bad epoch detection does not currently support event types other than word presentations, but could potentially
        be modified to do so.

        :return: None
        """
        ##########
        #
        # Create EEG epochs from raw data
        #
        ##########

        logger.debug('Identifying bad epochs for %s' % self.eegfile)

        # Create an mne events array with one row for each event of all types that appears in ev_ids (currently just
        # presentation events). The first column indicates the sample number of the event's onset, the second column is
        # ignored, and the third column indicates the event type as defined by the ev_ids dictionary.
        ev_ids = dict(
            WORD=0
        )

        offsets = [o for i,o in enumerate(self.events.eegoffset) if self.events.type[i] in ev_ids and self.events.eegfile[i].endswith(self.eegfile)]
        ids = [ev_ids[self.events.type[i]] for i,o in enumerate(self.events.eegoffset) if self.events.type[i] in ev_ids and self.events.eegfile[i].endswith(self.eegfile)]
        if len(ids) == 0:
            logger.warn('Skipping artifact detection for file %s due to it having no presentation events!' % self.eegfile)
            return
        mne_evs = np.zeros((len(offsets), 3), dtype=int)
        mne_evs[:, 0] = offsets
        mne_evs[:, 2] = ids

        tmin = 0.
        tmax = 1.6
        # Remove any events that run beyond the bounds of the EEG file
        truncated_events_pre = 0
        truncated_events_post = 0
        while mne_evs[0, 0] + self.eeg[self.eegfile].info['sfreq'] * tmin < 0:
            mne_evs = mne_evs[1:]
            truncated_events_pre += 1
        while mne_evs[-1, 0] + self.eeg[self.eegfile].info['sfreq'] * tmax >= self.eeg[self.eegfile].n_times:
            mne_evs = mne_evs[:-1]
            truncated_events_post += 1
        # Load data from all presentation events into an mne.Epochs object & baseline correct using each event's average voltage
        ep = mne.Epochs(self.eeg[self.eegfile], mne_evs, event_id=ev_ids, tmin=tmin, tmax=tmax, baseline=None, preload=True)

        ##########
        #
        # Individual-channel and all-channel bad epoch detection
        #
        ##########

        # Apply baseline correction on epoch data before analyzing individual channels across events
        ep.apply_baseline((0, None))

        # Method 1: High variance on individual channels during event
        variance = np.var(ep._data, axis=2)
        avg_variance = variance[:, self.eeg_mask].mean(axis=1)

        # Method 2: High median slope for individual channels during event
        gradient = np.gradient(ep._data, axis=2)
        gradient = np.median(gradient, axis=2)

        # Method 3: High voltage range on individual channels during event
        amp_range = ep._data.max(axis=2) - ep._data.min(axis=2)
        avg_amp_range = amp_range[:, self.eeg_mask].mean(axis=1)

        # Method 4: Large deviation of voltage from interquartile range on individual channels during event
        # Find the interquartile range of each channel, across time and across all events
        p75 = np.percentile(ep._data, 75, axis=[2, 0])
        p25 = np.percentile(ep._data, 25, axis=[2, 0])
        iqr = p75 - p25
        # Find the max and min of each channel during each event, then determine how many IQRs outside the IQR they fall
        amp_max_iqr = (ep._data.max(axis=2) - p75) / iqr
        amp_max_iqr[amp_max_iqr < 0] = 0
        amp_min_iqr = (ep._data.min(axis=2) - p25) / iqr
        amp_min_iqr[amp_min_iqr > 0] = 0

        # Mark entire events as bad if they have a high voltage range or variance across channels
        bad_epoch = np.logical_or(ss.zscore(avg_amp_range) > 3, ss.zscore(avg_variance) > 3)

        # Create events x channels matrices of booleans indicating whether each EEG channel is bad during each event
        eeg_art = np.logical_or.reduce((ss.zscore(variance, axis=0) > 3, ss.zscore(gradient, axis=0) > 3,
                                    ss.zscore(amp_range, axis=0) > 3, amp_max_iqr > 3, amp_min_iqr < -3))

        # Use only method 4 to search for blinks/eye movements in each EOG channel
        right_eog_art = np.logical_or(amp_max_iqr[:, self.reog_ind] > 3, amp_min_iqr[:, self.reog_ind] < -3)
        left_eog_art = np.logical_or(amp_max_iqr[:, self.leog_ind] > 3, amp_min_iqr[:, self.leog_ind] < -3)

        ##########
        #
        # Artifact Information Logging
        #
        ##########

        # Mark channels with artifacts during each presentation event
        logger.debug('Marking events with artifact info...')

        # Skip event types which have not been tested with artifact detection, and those aligned to other recordings
        event_mask = np.where([ev.type in ev_ids and ev.eegfile.endswith(self.eegfile) for ev in self.events])[0]
        # Also skip any events that run beyond the bounds of the EEG file
        event_mask = event_mask[truncated_events_pre:]
        event_mask = event_mask[:-truncated_events_post] if truncated_events_post > 0 else event_mask

        # badEpoch is True if abnormally high range or variance occurs across EEG channels
        self.events.badEpoch[event_mask] = bad_epoch
        # artifactChannels is a 128-item array indicating whether each EEG channel is bad during each event
        self.events.artifactChannels[event_mask, :self.n_chans-2] = eeg_art[:, self.eeg_mask]

        # variance is a 128-item array indicating the variance of each channel during the event
        self.events.variance[event_mask, :self.n_chans-2] = variance[:, self.eeg_mask]
        # medGradiant is a 128-item array indicating the median gradient of each channel during the event
        self.events.medGradient[event_mask, :self.n_chans-2] = gradient[:, self.eeg_mask]
        # ampRange is a 128-item array indicating the amplitude range of each channel during the event
        self.events.ampRange[event_mask, :self.n_chans-2] = amp_range[:, self.eeg_mask]
        # iqrDevMax is a 128-item array how many IQRs above the 75th %ile each channel reaches during the event
        self.events.iqrDevMax[event_mask, :self.n_chans-2] = amp_max_iqr[:, self.eeg_mask]
        # iqrDevMin is a 128-item array how many IQRs below the 25th %ile each channel reaches during the event
        self.events.iqrDevMin[event_mask, :self.n_chans-2] = amp_min_iqr[:, self.eeg_mask]

        # Set eogArtifact to 1 if an artifact was detected only on the left, 2 if only on the right, and 3 if both
        self.events.eogArtifact[event_mask] = 0
        self.events.eogArtifact[event_mask[left_eog_art]] += 1
        self.events.eogArtifact[event_mask[right_eog_art]] += 2

        logger.debug('Events marked with artifact info for %s' % self.eegfile)

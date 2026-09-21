"""Stop-list for glossary discovery (design doc 02, section 7.1 step 3).

Two sets, both matched case-sensitively against the token as it appears in the
text (the discovery only feeds Capitalized or acronym tokens here):

* ``STOP_WORDS`` — function words, common sentence starters, document-structure
  words (``Chapter``, ``Figure`` ...), months, weekdays, ordinals, interjections
  and a few frequent verbs/adjectives that are capitalized only because they open
  a sentence. An n-gram that *starts or ends* with one of these is discarded.
* ``HONORIFICS`` — titles that are discarded only when they stand alone
  (``Mr`` alone is noise, ``Mr Darcy`` is a term).

``Will``, ``May``, ``Mark``, ``Bill`` ... are deliberately absent: they are real
names and the ambiguity rule (7.1 step 5) handles their common-word reading.
"""

from __future__ import annotations

from typing import FrozenSet

STOP_WORDS: FrozenSet[str] = frozenset(
    {
        # articles, determiners, pronouns
        "A", "An", "The", "This", "That", "These", "Those", "Some", "Any", "Each", "Every",
        "All", "Both", "Either", "Neither", "No", "None", "Such", "Other", "Another", "Its",
        "I", "Me", "My", "Mine", "Myself", "We", "Us", "Our", "Ours", "Ourselves", "You",
        "Your", "Yours", "Yourself", "Yourselves", "He", "Him", "His", "Himself", "She",
        "Her", "Hers", "Herself", "It", "Itself", "They", "Them", "Their", "Theirs",
        "Themselves", "One", "Ones", "Oneself", "Who", "Whom", "Whose", "Which", "What",
        "Whatever", "Whoever", "Whichever", "Something", "Anything", "Nothing", "Everything",
        "Someone", "Anyone", "Everyone", "Nobody", "Somebody", "Anybody", "Everybody",
        # prepositions, conjunctions, particles
        "Of", "In", "On", "At", "To", "For", "From", "By", "With", "Without", "Within",
        "About", "Above", "Below", "Under", "Over", "Between", "Among", "Through", "Across",
        "Against", "Along", "Around", "Behind", "Beneath", "Beside", "Beyond", "Down", "Up",
        "Into", "Onto", "Out", "Off", "Near", "Past", "Per", "Since", "Than", "Till",
        "Toward", "Towards", "Upon", "Via", "Versus", "And", "Or", "But", "Nor", "So",
        "Yet", "If", "Then", "Else", "Because", "Although", "Though", "While", "Whereas",
        "Whether", "Unless", "Until", "After", "Before", "During", "Once", "As", "Not",
        "Only", "Even", "Just", "Also", "Too", "Very", "Quite", "Rather", "Almost",
        "Already", "Always", "Never", "Often", "Sometimes", "Usually", "Again", "Still",
        "Ever", "Here", "There", "Where", "Wherever", "When", "Whenever", "Why", "How",
        "However", "Whereby", "Thus", "Therefore", "Hence", "Moreover", "Furthermore",
        "Nevertheless", "Nonetheless", "Meanwhile", "Otherwise", "Instead", "Besides",
        "Anyway", "Indeed", "Perhaps", "Maybe", "Certainly", "Clearly", "Obviously",
        "Actually", "Probably", "Possibly", "Surely", "Apparently", "Finally", "Eventually",
        "Suddenly", "Immediately", "Later", "Earlier", "Soon", "Now", "Today", "Tomorrow",
        "Yesterday", "Tonight", "Recently", "Currently", "Generally", "Typically",
        "Especially", "Particularly", "Specifically", "Similarly", "Likewise", "Alternatively",
        "Consequently", "Accordingly", "Additionally", "Fortunately", "Unfortunately",
        "Interestingly", "Importantly", "Notably", "Basically", "Essentially", "Simply",
        "Naturally", "Ultimately", "Overall", "Together", "Alone", "Away", "Back", "Forward",
        "Ahead", "Inside", "Outside", "Elsewhere", "Everywhere", "Nowhere", "Somewhere",
        "Anywhere", "Please", "Yes", "Yeah", "Okay", "OK", "Oh", "Ah", "Well",
        "Hello", "Hi", "Hey", "Thank", "Thanks", "Sorry", "Sure", "Fine",
        "Many", "Much", "Few", "Several", "Most", "More", "Less", "Least", "Enough", "Same",
        "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth",
        "Tenth", "Last", "Next", "Previous", "Former", "Latter", "Following",
        "Own", "Certain", "Various", "True", "False",
        # auxiliaries and modals (names such as Will/May are intentionally absent)
        "Am", "Is", "Are", "Was", "Were", "Be", "Been", "Being", "Have", "Has", "Had",
        "Having", "Do", "Does", "Did", "Doing", "Done", "Would", "Should", "Could", "Can",
        "Cannot", "Might", "Must", "Shall", "Ought", "Need", "Dare", "Used", "Let", "Lets",
        # frequent sentence-opening verbs
        "Come", "Go", "Get", "Give", "Take", "Make", "Made", "Say", "Said", "Tell", "Told",
        "Think", "Thought", "Know", "Knew", "See", "Saw", "Look", "Looked", "Listen", "Wait",
        "Stop", "Keep", "Put", "Turn", "Try", "Ask", "Asked", "Answer", "Call", "Called",
        "Find", "Found", "Feel", "Felt", "Leave", "Left", "Live", "Lived", "Mean", "Meant",
        "Move", "Moved", "Play", "Run", "Ran", "Seem", "Seemed", "Show", "Showed", "Sit",
        "Sat", "Stand", "Stood", "Start", "Started", "Use", "Want", "Wanted", "Work",
        "Worked", "Write", "Wrote", "Read", "Remember", "Consider", "Suppose", "Imagine",
        "Note", "Notice", "Recall", "Assume", "Begin", "Began", "End", "Ended",
        "Open", "Close", "Hold", "Held", "Bring", "Brought", "Set", "Cut", "Hear", "Heard",
        "Understand", "Understood", "Believe", "Hope", "Wish", "Like", "Love", "Hate",
        "Walk", "Walked", "Talk", "Talked", "Speak", "Spoke", "Watch", "Learn", "Return",
        "Returned", "Follow", "Followed", "Meet", "Met", "Lead", "Led", "Fall", "Fell",
        "Rise", "Rose", "Grow", "Grew", "Die", "Died", "Kill", "Killed", "Help", "Helped",
        "Send", "Sent", "Reach", "Reached", "Pass", "Passed", "Happen", "Happened",
        # document structure words
        "Chapter", "Chapters", "Part", "Parts", "Section", "Sections", "Figure", "Figures",
        "Fig", "Table", "Tables", "Page", "Pages", "Book", "Books", "Volume", "Volumes",
        "Appendix", "Index", "Preface", "Foreword", "Introduction", "Conclusion", "Contents",
        "Summary", "Abstract", "Glossary", "Bibliography", "References", "Notes", "Epilogue",
        "Prologue", "Afterword", "Acknowledgements", "Acknowledgments", "Dedication",
        "Example", "Examples", "Exercise", "Exercises", "Problem", "Problems", "Solution",
        "Solutions", "Step", "Steps", "Item", "Items", "Point", "Points", "Line", "Lines",
        "Paragraph", "Edition", "Copyright", "Published", "Printed", "ISBN", "Source",
        "Illustration", "Plate", "Diagram", "Chart", "Map", "List", "Box", "Case", "Question",
        "Definition", "Theorem", "Lemma", "Proof", "Corollary", "Remark", "Equation",
        "Warning", "Caution", "Tip", "Hint", "Important", "Reference",
        # months, weekdays, seasons
        "January", "February", "March", "April", "June", "July", "August", "September",
        "October", "November", "December", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug",
        "Sep", "Sept", "Oct", "Nov", "Dec", "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday", "Mon", "Tue", "Wed", "Thu", "Fri", "Sun",
        "Spring", "Summer", "Autumn", "Winter",
        # numbers and units
        "Zero", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Eleven", "Twelve", "Twenty", "Thirty", "Forty", "Fifty", "Hundred", "Thousand",
        "Million", "Billion", "Dozen", "Twice",
        # common acronyms that are not terms
        "AM", "PM", "BC", "AD", "BCE", "CE", "TV", "PS", "NB", "ID", "TL", "DR",
        "FAQ", "PDF", "URL", "HTML", "XML", "JSON", "CSV", "ASCII", "UTF",
        "USA", "UK", "US", "EU", "UN", "AI", "IT",
    }
)

HONORIFICS: FrozenSet[str] = frozenset(
    {
        "Mr", "Mrs", "Ms", "Miss", "Mx", "Dr", "Prof", "Professor", "Sir", "Madam", "Madame",
        "Lady", "Lord", "Master", "Mister", "Saint", "St", "Rev", "Reverend", "Father",
        "Mother", "Brother", "Sister", "Captain", "Capt", "Colonel", "Col", "General", "Gen",
        "Major", "Sergeant", "Sgt", "Lieutenant", "Lt", "Admiral", "Commander", "Officer",
        "President", "Senator", "Governor", "Mayor", "Judge", "King", "Queen", "Prince",
        "Princess", "Duke", "Duchess", "Earl", "Count", "Countess", "Baron", "Baroness",
        "Emperor", "Empress", "Chief", "Doctor", "Nurse", "Uncle", "Aunt", "Grandma",
        "Grandpa", "Mom", "Dad", "Mama", "Papa",
    }
)

STOP_TOKENS: FrozenSet[str] = STOP_WORDS | HONORIFICS

# About this article

I published [Learning in Reverse](learning_in_reverse.md) nearly a year ago. The thesis was simple: you can start with output, work backwards through understanding, and learn more effectively than the traditional staircase model suggests. I still believe that. For the most part.

Things have moved fast enough that it's worth revisiting. Not to retract the argument, but to examine what's happened to it since.

---

## The Shortcut Still Works. We Just Stopped Using It.

When I wrote Learning in Reverse, I was working through something personal. I'd spent years convinced I couldn't code. Then I approached it differently, using AI to generate working output, breaking it, fixing it, learning backwards through the wreckage. In a few days I had something real. The mechanism worked.

What I didn't examine closely enough was *why* it worked. I attributed it to the model: start with Apply, skip the staircase, let the output teach you. That's true as far as it goes, but there was something else happening that I didn't name at the time.

The friction was doing the work.

Every time I copied code from a chat window into an editor, I had to read it well enough to know where it went. Every time something broke, I had to understand the complaint before I could fix it. Every time I ran a version that almost worked, I was building a map of the territory without knowing I was doing it. The learning wasn't in the AI. It was in the gap between the AI's output and a working result. That gap was the curriculum.

On reflection I already knew this. I've said to specialists many times while working on learning content together: "The day you learned the most was the day when everything was profoundly broken." In a weird way, AI allowed me to build many broken states.

Then the tools closed the gap.

Cursor. GitHub Copilot. Claude Code. Agentic pipelines that read your codebase and modify it directly. The code no longer needs to travel through your understanding to get from the AI to the editor. The friction is gone. With it, so is the mechanism.

The shortcut in coding is no longer a shortcut. It's a bypass. You don't learn in reverse anymore. You just don't learn.

---

## Coding Is Not A Special Case

My first instinct was to treat this as a coding-specific problem. The tooling around software development has always moved in this direction: IDEs, frameworks, Stack Overflow, package managers. I think back to a conversation I had with my uncle years ago who was complaining that the art of coding was lost because programmers didn't need to write elegant, efficient code anymore to fit everything into 16KB. I suppose that pipeline is just reaching another conclusion. You don't need creative elegance anymore. Maybe you don't need to write code anymore.

AI seems to have just completed a journey that was already underway. Coding is different, I told myself.

I don't believe that anymore.

Coding is a preview. The mechanism that broke the shortcut there isn't specific to code. It's the collapse of the gap between intent and output. You describe what you want. The thing appears. You no longer have to translate your understanding into the result. That translation was where the learning happened.

That gap is closing in every domain AI is touching. Coding just got there first because it's the domain AI was built by, and for, and in. Other domains will follow, at different speeds, in different shapes. The question isn't whether coding is a special case. It's which domain is next, and whether anyone will notice in time.

There is a fascinating philosophical conversation here that sits outside the bounds of this article. When you, a human, write anything, you think about what you're going to write, you mentally structure what needs to come first, then next, you form the narrative as a series of ideas swirling around your brain. You probably had a few key words or phrases and you start to write it out. LLMs operate differently. The output of a single word is a probabilistic function of the last word, and given how modern LLMs work, every word that came before it. LLMs therefore represent a true collapse of intent and output. That phrase carries real meaning in this context.

---

## The Toll Road Nobody Told You About

Here's something I've been sitting with for a few months.

Coding isn't one skill. It's at least three. There's syntax, the mechanical expression of logic in a language a compiler accepts. When learners think "I'm going to learn to code," this is the skill they think of. What is rarely discussed is that on top of that there's data flow, understanding how information moves, transforms and persists through a system, and there is system design, the higher-order reasoning about architecture, tradeoffs and consequence.

For decades, syntax was the gatekeeper. You couldn't get near data flow or system design without paying the toll. The toll was years. Years of wrestling with semicolons and scope and type errors and segfaults and all the other small ways a system tells you that you haven't understood it yet.

Nobody framed it as a curriculum. It didn't feel like one. It felt like friction, but that friction was the thing building the intuition that data flow and system design actually require. You weren't just learning syntax. You were developing a mental model of how systems behave, slowly, through the back door, through sheer accumulated failure.

AI didn't just lower the barrier to coding. It removed the incidental curriculum that the barrier was running. Syntax was the toll road. The toll road was also the training ground. They were the same thing, and we didn't know it until one of them disappeared.

Every complex domain probably has this structure: a visible, frustrating gatekeeping skill sitting in front of less visible but more important ones. Coding just makes the structure unusually legible. Which is why it's worth paying attention to.

---

## Operates vs. Works

This is where the original Lab/Production split still holds, but something underneath it has shifted.

In the original article I talked about Bloom's taxonomy divided across a risk boundary: the lab side where AI can lead, and the production side where human judgment is non-negotiable. That split is still real, but there's a more fundamental distinction forming underneath it.

There are people who can operate a system. They understand its behaviour well enough to get useful output. They know the inputs that produce the results they need. This is a genuine skill. It's not trivial.

There are people who understand how a system works. They have an internalised model of the mechanism. They can reason about failure modes that haven't occurred yet. They can recognise when a confident output is structurally wrong. They know what the system would do in the edge case that hasn't been documented, because they understand why it does what it does.

AI-assisted learning develops operates naturally. The feedback loop is immediate, the outputs are validating, the path is smooth. Works requires something different: the compulsion, or the discipline, to not accept the output until you understand why it is what it is.

The problem isn't that people can't reach works anymore. It's that nothing in the current environment requires them to. Output is the metric. Output is visible. The absence of underlying understanding is invisible until the moment it isn't.

---

## The Failure You Won't See Coming

In safety-critical engineering, there's a concept worth borrowing here: latent failure. The system operates normally. All the visible indicators are green. The deficit is structural, hidden, and will only surface when the system encounters a condition that exposes the absence of something that should have been there.

The expertise gap we're building right now has that shape.

The novice using AI-assisted tools is producing credible output. Their manager sees credible output and validates them. The organisation's metrics show productivity. The feedback loops that would previously have surfaced a knowledge gap, the months of struggle, the visible confusion, the questions that revealed what wasn't understood, those signals don't fire. The deficit forms without anyone registering it.

In a decade, some of those people will be senior practitioners. They'll be reviewing others' work, making judgment calls at the edge of consequence, handling the ambiguous situations that require exactly the internalised pattern library they never built. The people who built theirs the old way will be gone, or moving towards the exit.

The expertise famine isn't visible yet. The system currently looks fine. That's what makes it worth talking about now, while it might still be possible to do something about it.

---

## The Shortcut Is Still There

Here's the thing I want to be clear about.

The shortcut isn't broken. Not in general. Outside of the domains where the tooling has specifically engineered away the need to learn, the mechanism still works. Start with output. Break it. Learn backwards through the understanding that the breaking requires. It's still real. The original thesis stands.

What's changed is the choice.

The faster path exists now in more and more domains. You can get to the output without making the journey. The output looks the same, which means the organisations measuring output can't tell the difference, and many of the individuals producing it can't either.

So the shortcut isn't deprecated. It's abandoned. Not through any deliberate decision. Not through any policy or consensus or reasoned argument. Just through the accumulated weight of individual efficiency decisions, each one rational, none of them accounting for what the friction was quietly doing on the way through.

We're not choosing not to build expertise. We're just choosing the faster path. Repeatedly. Everywhere. Until the expertise that the slower path was building stops being produced.

Nobody made that choice. It's just what happened.

---

## Some Intellectual Honesty

I should say something directly here, because it's relevant.

Blueprinted.io, the platform I've been building alongside this thinking, was made with significant AI support. I have nearly twenty years in learning design. I do not have twenty years in software engineering. The codebase exists because the shortcut worked well enough to produce something real, something I can point at and say to someone with deep specialism: build this properly.

It's open source for a few reasons, and the main one is on the website: protecting the ruleset, preventing the kind of SaaS capture that would undermine what the project is actually for. There is, in fact, another reason I'm less public about. I'm genuinely uncomfortable attempting to commercialise something that isn't a product of my own technical understanding. I know what it should do. I understand the systems logic underneath it. I do not fully understand the code that expresses it. That gap matters to me, which is why I'm actively looking for people who are specialists in the systems side to contribute, people for whom the code is not a shortcut but a foundation.

What AI did for Blueprinted is something the original article didn't quite articulate. For a domain expert, it doesn't replace technical expertise. It lets you build a communicable artefact of your intent. Without the shortcut, Blueprinted would still be a document, or a diagram, or eighteen months of trying to explain L&D architecture to an engineer who doesn't understand the problem domain. Instead it's a working thing. That's not nothing. Knowing what it is and knowing what it isn't are both important.

For learning specialists this dichotomy is extremely well understood. Every single LMS on the market can be placed into one of two categories: built by engineers who didn't understand learning well enough, or built by learning specialists who didn't understand the engineering well enough. Balancing those two has been the holy grail of LMS design and I've never seen anyone crack it.

---

## What The Shortcut Taught Me About Myself

There's one more thing the shortcut did that I didn't expect.

Working through Blueprinted, pushing into systems I didn't fully understand, I discovered something I didn't know to look for. It turns out I'm reasonably good at systems level thinking. Data flow, architecture, the shape of how information moves through a process. I can reason about those things without being able to express them in code directly.

That's not a skill I knew I had. It's not a skill that twenty years of L&D gave me any obvious reason to discover. The syntax barrier wasn't protecting that skill. It was just sitting in front of a context where the skill could finally become visible.

Which complicates the toll road argument in an interesting way. For some people the gatekeeping isn't protecting the deeper skill at all. The deeper skill is already there, developed through completely different experience, waiting for an environment that makes it legible. The AI didn't teach me systems thinking. It handed me a situation where I could finally see I already had it, or if not, the skill was certainly motivated to continue by flexing that muscle.

I don't know how common that is, but it's worth naming, because the operates/works model I described earlier assumes a fairly linear relationship between foundation and capability. The reality, at least in this case, was messier and more surprising than that.

---

## What This Means

I don't have a clean answer to this. I'm not sure one exists yet.

What I do think is that L&D as a profession is not treating this as urgent. The conversation tends to focus on how AI can be integrated into learning design: which tools, which models, which workflows. That's the wrong level of analysis. The more important question is which domains still have productive friction in them, whether we understand what that friction is building, and what happens when it's gone.

The domains that matter most, the ones involving ambiguity, judgment, consequence, are the ones where deep expertise is genuinely irreplaceable. They're also the ones where the conditions for producing that expertise are quietly deteriorating.

If you're responsible for building capability at scale, the question worth sitting with isn't how to make learning more efficient. It's whether the efficiency you're optimising for is consuming the thing you actually need.

Nobody made that choice. It's just what happened.

> *The expertise isn't disappearing because we can't build it anymore.  
> It's disappearing because we keep choosing not to.  
> One efficiency gain at a time.*

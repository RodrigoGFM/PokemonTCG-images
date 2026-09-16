import { Card } from "../../../interfaces"
import Set from "../30th Celebration"

const card: Card = {
	set: Set,

	name: {
		en: "Exeggcute",
		fr: " ",
		es: "Exeggcute",
		'es-mx': "Exeggcute",
		de: " ",
		it: " ",
		pt: " "
	},

	illustrator: "Nelnal",
	rarity: "Common",
	category: "Pokemon",
	dexId: [102],
	hp: 60,
	types: ["Grass"],
	stage: "Basic",

	attacks: [{
		cost: ["Colorless"],

		name: {
			en: "Hypnosis",
			fr: " ",
			es: " ",
			'es-mx': " ",
			de: " ",
			it: " ",
			pt: " "
		},

		effect: {
			en: "Your opponent's Active Pokémon is now Asleep",
			fr: " ",
			es: " ",
			'es-mx': " ",
			de: " ",
			it: " ",
			pt: " "
		},

		damage: 
	}],

	weaknesses: [
		{
			type: "Fire",
			value: "x2",
		},
	],
	retreat: 2,
	regulationMark: "J",

	description: {
		en: "Using telepathy only fellow Exeggcute can pick up on, they always form a clister of six.",
		de: " "
	},

	variants: [
		{
			type: "holo",
			thirdParty: {
				cardmarket: ,
				tcgplayer:
			}
		},
		
	],
}

export default card